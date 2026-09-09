# OpenClaw 9.x: founder runtime upgrade and multiplayer delivery plan

Date: 2026-09-08. Status: research-backed proposal; not an implementation or deployment record. Evidence: current GitHub source and tagged upstream releases; live production validation remains an explicit execution gate.

## Recommendation

Upgrade and qualify the fork first, deploy a reversible founder-only runtime canary second, and expose the product capabilities in independently gated slices. Preserve the existing fleet default until the founder release is proven. Do not equate an image upgrade with a completed Lightbulb feature.

The researched target is upstream `v2026.9.3`, currently the latest stable release. Recheck stable releases when implementation starts, then freeze the chosen tag and commit for qualification. Do not follow upstream main or a floating latest tag in production. [Upstream release](https://github.com/openclaw/openclaw/releases/tag/v2026.9.3).

Current repository pins are fork base `2026.8.1-isol8.3` and extended dev/prod image `2026.8.1-isol8.3-4e22eb8`. These are source configuration, not proof of the image currently running in the founder container. No live AWS inventory or 9.x boot was performed for this plan. [Version manifest](https://github.com/TheLightbulbCompany/lightbulb/blob/main/openclaw-version.json).

The intended product is a network of people, durable agents, conversations, and authorized execution devices. A conversation does not require its own container. An OpenClaw node is an execution location, not automatically a human, agent, or separate private workspace.

Two gates matter before exposing multiplayer:

1. Native human identity: the current shared owner-level backend connection cannot represent distinct people to OpenClaw. Prove a compatible authenticated identity path without breaking Paperclip, health checks, or desktop nodes.
2. Sharing semantics: upstream members are participants, not an invite-only read ACL. Start new founder collaboration with explicitly company-readable rooms among trusted colleagues. Preserve existing private histories. Confidential invite-only shared rooms require an explicit policy extension or an isolated runtime boundary.

This document separates source-verified behavior, proposed implementation, and acceptance gates. Nothing here authorizes or claims a production rollout has happened.

## 1. What actually comes from the image upgrade?

“Runtime included” means the selected fork image contains the implementation, with compatible plugins installed where required. It does not mean the capability is enabled, correctly configured, reachable through Lightbulb, or rendered by our clients.

| Capability | Upstream provenance | What the upgraded runtime supplies | What Lightbulb must deliver |
|---|---|---|---|
| Shared conversations | Already in 8.1 | Native visibility and participation policy, membership methods, session subscriptions | Verified human identities, room navigation, authorized transport, clear audience labels, concurrency handling |
| Ownership, presence, typing | Already in 8.1 | Creator/current-owner metadata and presence/typing mechanisms | Separate creator from assignee in UI; member/client connections; avatars and live state |
| Specialist delegation and yield | Already in 8.1 | Spawn, yield, run settlement, parent/child lineage | Visible handoff cards, continuation/error/cancel behavior, Paperclip links; prove delivery after yield |
| Persistent side conversations | Already possible in 8.1; refined in 9.x | Durable visible child sessions distinct from temporary subagent runs | Session-aware panels, history/deep links, parent navigation, steer/continue controls |
| Device tools, node MCP and skills | Already in 8.1 | Paired execution nodes and node-provided capabilities | Member authorization, explicit device selection, offline handling, approval and routing tests |
| Full node-hosted sessions | Already opt-in in 8.1; repository-backed placement improves in 9.x | Worker/session placement primitives | Worker ingress, compatible node runtime, placement UX and recovery; not automatic with existing browser routing |
| Swarm | Experimental opt-in in 8.1; enabled by default in 9.2 | Collector-style parallel work and result gathering | Explicit rollout flag, bounded fanout/depth, cost controls, progress/cancellation; no assumption of arbitrary persistent distributed workers |
| Code Mode | Experimental opt-in in 8.1; execution improves in 9.3 | Scripted tool composition | Model/tool compatibility tests, approval/billing propagation, useful live/history rendering |
| Human mentions | Added in 9.2 | Mention targets and bounded native mention inbox | Composer picker, access-aware navigation, notification delivery, mobile behavior |
| Personal model accounts | Per-person flows in 9.2; management/order controls in 9.3 | Profile-bound connection and selection APIs | Secure connection UI, account ownership/payer display, fallback policy, reconnect/cancel handling |
| Public conversation links | Added in 9.3 | Revocable publication grant for existing and future conversation text | Deliberate publication/read routes, sanitized renderer, confirmation and revocation/cache tests |
| Skill Workshop | Existing feature; agent-owned storage changes in 9.3 | Agent-level collection and migration behavior | Durable EFS storage adaptation, migration validation, existing proposal/review UI integration |
| Slack controls | Newer session stop/title integration in 9.x | Native channel event handling | App-manifest subscriptions, backend/channel tests, correct Lightbulb session/title presentation |
| Runtime performance/recovery | New fixes in 9.x | Cache, lifecycle and recovery changes | Final-image benchmarks, config correctness and migration/restart qualification |
| Rich diagrams, live browser and model controls | Upstream client improvements | Relevant upstream UI/runtime assets | Separate Lightbulb React/Tauri/Expo work; upstream Control UI is disabled in our deployment |
| Meeting library and Team Reports | Additional 9.3 surfaces; Team Reports is optional | Feature/plugin implementation when installed and configured | Dedicated integration decision, data-source consent, persistence, authorization and Lightbulb screens |
| Invite-only confidential shared rooms | Not supplied by the native members list | No general selective read ACL through native membership alone | Deliberate extension of authoritative sharing policy, or suitable runtime separation |
| Cross-company agent network | Not a general shipped gateway federation layer | Useful sessions, agents and node primitives | Lightbulb discovery, explicit trust grants, scoped task/result exchange, revocation, budgets and audit |

The 8.1 baseline is important: we already have much of the engine. The missing work is often our integration rather than an upstream feature that first appears in September. Compare the tagged [8.1 method descriptors](https://github.com/openclaw/openclaw/blob/v2026.8.1/src/gateway/methods/core-descriptors.ts) and [9.3 descriptors](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/gateway/methods/core-descriptors.ts). See also tagged [8.1 multi-user](https://github.com/openclaw/openclaw/blob/v2026.8.1/docs/concepts/multi-user.md), [subagents](https://github.com/openclaw/openclaw/blob/v2026.8.1/docs/tools/subagents.md), [Swarm](https://github.com/openclaw/openclaw/blob/v2026.8.1/docs/tools/swarm.md), [Code Mode](https://github.com/openclaw/openclaw/blob/v2026.8.1/docs/tools/code-mode.md), and [nodes](https://github.com/openclaw/openclaw/blob/v2026.8.1/docs/nodes/index.md). September deltas are documented in [9.2](https://github.com/openclaw/openclaw/releases/tag/v2026.9.2) and [9.3](https://github.com/openclaw/openclaw/releases/tag/v2026.9.3).

## 2. Architecture to preserve and extend

| Layer | Responsibility |
|---|---|
| Clerk + Lightbulb backend | Human identity, organization membership, tenant routing, product entitlements, request admission and spend controls |
| OpenClaw runtime | Canonical sessions/transcripts, native human profiles, agent execution, sharing policy, model connections, node routing |
| Paperclip | Durable agents, assignments, issues, runs and deliverables; the work control plane, not a second transcript store |
| Lightbulb clients | Web, Tauri and Expo presentation of the same session identities, authors, relationships and controls |
| ECS/EFS/local SQLite/Litestream | Owner-scoped runtime lifecycle and recoverable durable state |
| Paired desktop/worker nodes | Explicitly authorized execution locations with bounded access |

Current Lightbulb pools an owner-scoped privileged backend connection. Current chat keys and event filters intentionally isolate member-owned chat paths; workspace panel identity is largely agent-based. Those decisions must be adapted explicitly, not removed wholesale.

The existing default-deny browser RPC allowlist, approval capability negotiation, and run/compaction/plan/tool activity handling are already on main. Preserve and extend them; verify deployment rather than rebuilding them. [Gateway connection pool](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/backend/core/gateway/connection_pool.py), [browser proxy](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/backend/routers/websocket_chat.py).

No new container per chat, no replacement of ECS with upstream Fleet, and no second autonomous scheduler competing with Paperclip. Keep the current Paperclip base release unless an integration requirement proves an upgrade necessary. Native OpenClaw Fleet is not our multi-tenant SaaS control plane.

### The privacy contract

Use these distinct product concepts:

- Personal conversation: access restricted through a verified private-session policy; never silently broaden old chat visibility.
- Company room: explicitly readable by the trusted organization audience, with participation rules shown separately.
- Invite-only shared room: a future supported confidential audience, requiring a real selective read policy before it can be labeled private.
- Published transcript: explicitly public to anyone possessing its link, including future conversation text until revoked.

Upstream non-draft sessions are generally readable by identified users; membership changes participation. A broad foreign-session write role grants additional access, while a foreign-session “none” role blocks foreign sessions rather than expressing selective membership-based reading. Creator-protected drafts are not private group rooms. Assigning a human/agent owner changes responsibility, not the creator's sharing authority. [Native sharing policy](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/gateway/session-sharing-policy.ts), [read and event filtering](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/gateway/session-sharing.ts).

Before admitting native human profiles to existing state, inventory legacy sessions and prove the creator/visibility migration. Sessions without explicit native visibility may default to shared. Unknown historical authors must remain unknown, not be fabricated from a display name. Any unresolved legacy private history remains unexposed; no company-wide session browsing until this gate passes.

Private transcripts are not a guarantee against an unrestricted agent, runtime administrator, shared filesystem or shared memory. Current generated memory-wiki configuration has a global vault/bridge; this is a boundary to evaluate, not proof of an observed leak. Confidential contexts need tool/memory/storage policy and, for stronger isolation, separate runtime credentials and execution boundaries. Do not market per-person accounts as OS-level secret isolation.

## 3. Runtime delivery: fork first, correct configuration, then founder production

### R0 — Freeze the target and capture the actual founder baseline

Deliver a release evidence manifest containing:

- Frozen upstream tag/commit, fork starting tag and all carried patch commits.
- Exact founder organization/owner, container row, ECS service/task definition, Paperclip company and member roster.
- Actual running image reference/digest, OpenClaw version, task size/role/mounts, node versions, provider/channel/plugin inventory.
- Current config hash, shared and per-agent DB inventory, Workshop state, relevant durable files and protected secret references.
- Existing prompt latency, start/recovery timings, idle/active memory, concurrency, failure rate and representative task cost.

Resolve the owner explicitly. The present admin helper can select the first organization and fall back to a personal user on Clerk failure; neither is acceptable migration targeting. Do not reuse a founder ID copied from an old document.

Exit: one unambiguous target and a reproducible before-state. A repository pin or admin “applied” label is not runtime evidence.

### R1 — Rebase the fork and qualify every carried behavior

Work in an isolated fork branch based on the frozen stable tag. Preserve existing release refs. Create a patch ledger: each existing patch is kept, adapted, or retired with a regression test and upstream evidence.

Pay particular attention to:

- Fork-only MCP runtime-scope and browser session-state schema/behavior.
- Provider/model policy and credential handling.
- Shell-reference-preserving transcript redaction and literal-secret redaction across chunk boundaries.
- SQLite, WAL/checkpoint and missing-store behavior. Retire a patch only when the replacement passes its behavioral test.
- Plugin SDK changes, structured search/list result changes, and typed MCP prompt responses.
- Supported Node runtime in the final image and separately on node-hosted CLI installations.

Rebuild and exercise in-repo plugins: aws-exec-profile, paperclip-exec-creds and lightbulb-marketplace. Rebuild checked-in generated plugin output when required. Resolve compatible published versions for Slack, Discord, Bedrock provider, Lobster, Parallel, Firecrawl, ACPX and Tokenjuice; do not assume all package versions match the host tag. Keep independent third-party versions independent unless compatibility requires a change.

Run an early identity compatibility prototype during this work. If a narrowly scoped auth bridge is needed, identify it before promising the first founder image is the final multiplayer-ready fork.

#### Workshop persistence is a release blocker

In 9.3 the Workshop root derives from agentDir. Our agent databases intentionally live on local disk with Litestream replication. Moving all of agentDir onto EFS would also move at least auth-profile SQLite storage and may split path behavior among callers.

Preferred proposed adaptation: keep agentDir/SQLite local; introduce one narrowly scoped Workshop root resolver pointing to a durable per-agent EFS directory. Use it consistently for migration, reads/writes, discovery, watches and collection ownership. Preserve path containment and symlink protections. This is a proposed fork patch, not a claimed upstream setting.

Tests: migrate applied and pending proposals; handle conflicts conservatively; approve/update/reject/restore; create a new agent; replace the ECS task twice; confirm skills survive, ownership is unchanged, and no SQLite DB appears under the Workshop file root. [Workshop root](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/skills/workshop/skills-root.ts), [auth-profile DB path](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/agents/auth-profiles/sqlite.ts), [skill write safeguards](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/skills/lifecycle/workspace-skill-write.ts).

Exit: buildable fork, patch ledger, behavioral regression results, storage solution and candidate release provenance. Publish a new immutable fork release only after checks pass.

### R2 — Make mixed-version configuration and founder assignment correct

This work must land before changing the global build base in ways that affect existing version gates.

1. Add a small persistent per-owner release assignment. A central resolver returns the owner's image, explicit upstream/fork version, schema identity and supported capabilities; otherwise it returns the environment default.
2. Use that resolver everywhere a task definition or config can be created: initial provisioning, image update, resize/provider changes, recovery, stopped-service recreation, wake/reprovision and admin regeneration.
3. Support both the old and candidate schema artifacts. Select by the resolved owner/target release, including validation and pruning. Do not replace the only schema globally while 8.1 owners remain.
4. Audit global version checks and “newer base implies every fork patch” assumptions. Retired custom keys require explicit capability changes.
5. Separate target config from active config. Generate and validate candidate config without exposing it to the running 8.1 watch/reload path. Activate only after the old writer stops.
6. Record rollout phases such as staged, drained, activating, validating, healthy and failed; reuse existing update records where feasible. Current image apply returns “applied” before ECS/gateway acceptance.
7. Keep an explicit version separate from image digest. Existing tag parsers do not support digest-only references safely. Use immutable tags plus verified digests until the resolver fully supports digest references.
8. Extend schema vendoring to identify the actual checked-out fork revision and preserve artifacts for all supported releases.

Current code seams: [version gates](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/backend/core/openclaw_version.py), [config schema validation](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/backend/core/services/config_schema.py), [schema vendoring](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/backend/scripts/vendor_openclaw_schema.sh), [update service](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/backend/core/services/update_service.py), [ECS lifecycle](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/backend/core/containers/ecs_manager.py), [admin targeting](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/backend/core/services/admin_service.py).

Initial candidate config should preserve current behavior deliberately:

- Keep Swarm disabled until its feature gate; bound spawn depth and concurrency explicitly.
- Preserve narrow session/tool visibility and A2A policy initially. Upstream 9.x defaults are broader; do not inherit them accidentally.
- Validate every fork-specific key, model policy, provider credential route, channel and plugin against the candidate schema and actual plugin loader.
- Introduce 9.x-only MCP TTL or cron options only for supported owners. A cron missed-job policy does not configure Paperclip scheduling.
- Evaluate hostname blocks without breaking required backend/provider connections. Tool-level SSRF settings are not a general shell/network egress boundary.
- Measure config hot-apply/restart behavior, including batched environment updates; do not infer restart counts from the changelog.

Exit: tests prove that one owner can stay on 9.3 while other and newly provisioned production owners remain on 8.1, including resize and stopped-service recreation. Both schemas reject invalid config rather than silently disabling validation.

### R3 — Build the extended image and prove it in dev

Build from the qualified fork, then use the existing authenticated GHCR-to-ECR extended-image workflow. Keep build-base changes distinct from environment promotion.

- Confirm the final image includes correct plugins, scripts, schema expectations, durable-path adaptation, permissions, browser/MCP dependencies and compatible Node.
- Preserve SOCI v2 publication. Record the resulting ECR manifest digest and actual CI-produced tag; do not reconstruct the suffix or confuse pre-SOCI local digest with deployed digest.
- Run final-image tests as the runtime user. A successful build or a cache-warming doctor command whose failure is ignored is not a startup test.
- Test both a brand-new dev owner and a sanitized representative founder-state migration. Disable production triggers and consequential external actions in the migration fixture.
- Cover Friday, Atlas and a representative larger agent roster, provider discovery, real streamed prompts, approvals/questions, MCP auth and calls, browser/live view, desktop nodes, Slack, Paperclip dispatch/results, park/wake and task replacement.
- Measure restore time, doctor/migration time, gateway-ready time, lazy image fetch behavior and task memory separately. Do not reuse stale image-size or full-pull estimates.

[Extended image build](https://github.com/TheLightbulbCompany/lightbulb/blob/main/.github/workflows/build-openclaw-image.yml), [Dockerfile](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/infra/openclaw/Dockerfile), [local SQLite boot](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/infra/openclaw/local-sqlite/isol8-local-sqlite-boot.sh).

Exit: candidate provenance, clean and migrated boot evidence, integration results, replacement durability and successful rollback rehearsal.

### R4 — Founder-only production canary

Deploy backward-compatible backend/schema/lifecycle changes first. Keep prod's environment default at the current stable extended image; assign the candidate only to the explicitly verified founder owner. Reuse the existing admin image-update workflow after the R2 fixes rather than inventing a parallel deployment mechanism.

Sequence:

1. Display the exact target, before/after release, backup location and expected interruption. Confirm the founder's maintenance window before production execution.
2. Pause new chat admission and relevant external dispatch sources; drain or deliberately cancel active work. Include Paperclip, OpenClaw cron, channel events and webhooks.
3. Stop the old single writer and verify final replica sync/task stop.
4. Capture the consistent recoverable state set described below.
5. Activate staged config and release assignment, then start the candidate task.
6. Verify running image digest, migration outcome and gateway readiness. Doctor timeout/failure must fail the migration gate even if today's boot script continues.
7. Complete real founder prompts and the critical integration checks; inspect errors, routing, spend and state.
8. Re-enable dispatch sources in a controlled order and reconcile paused/in-flight work.
9. Exercise graceful restart, abrupt replacement, park/wake and one lifecycle path that rebuilds the task definition.
10. Observe a normal work cycle; a suggested minimum is 24 hours plus the scripted scenarios, not 24 hours of idle uptime.

Do not declare success based only on ECS RUNNING, a successful health endpoint, or the update row's “applied” status.

Exit: the founder is actually running the candidate, remains pinned across lifecycle changes, retains state and existing functionality, and has a verified recovery path. The rest of production is unchanged.

### State-aware rollback, not merely an old image

The current single-writer deployment does not provide automatic ECS circuit-breaker rollback. Local SQLite replicas use the owner's live S3 prefix; an EFS archive alone is incomplete.

Before migration, preserve a coherent recovery set: shared DB, every per-agent DB, config, relevant EFS files/Workshop state and release manifest. Use an isolated recovery prefix or generation so 9.x replication cannot replace the only old restore point. Protect backup credentials and access.

Rehearse restoration before founder rollout. Restore the old image assignment, matching config/files and compatible DB set; make sure the next boot cannot pick up the newer replica history. Verify actual old-runtime reads and a real task.

After founder usage begins, rollback is a recovery-point decision. Preserve and account for post-upgrade transcripts/files and Paperclip task/run links before restoring older state. Do not overwrite new work silently. Prefer forward repair when safe; reconcile pending external actions and do not replay consequential work blindly.

A UI feature can usually be disabled independently. Database/image rollback is a separate operation.

## 4. Product delivery after runtime qualification

### F0 — Prove human identity and safe legacy-session handling

This is the technical prerequisite, not a new user-facing feature inserted ahead of rooms.

Target: separate owner-service connections from authenticated human connections, keyed by owner plus a verified durable member identity. Decide through testing whether presence/subscription state requires per-client connections within each member.

Lightbulb verifies Clerk identity and current organization membership, derives an issuer-qualified immutable ID, strips client-supplied identity headers, and grants only server-selected scopes. Preserve request admission, idempotency, billing and method authorization.

Do not put all humans behind the privileged owner token and add a cosmetic sender field. OpenClaw derives authorship from the authenticated profile; chat.send does not accept an arbitrary trusted author override.

Prove upstream trusted-proxy compatibility in dev. It rejects a configured shared token; current operator RPC, desktop node relay and Paperclip paths depend on token-authenticated connections. A config-only switch is unsafe. Test a supported internal service-auth path where compatible; otherwise design a constrained fork bridge. Do not expose an admin HTTP/RPC path publicly to avoid this work. [Trusted-proxy contract](https://github.com/openclaw/openclaw/blob/v2026.9.3/docs/gateway/trusted-proxy-auth.md), [native authorship](https://github.com/openclaw/openclaw/blob/v2026.9.3/src/gateway/server-methods/gateway-client-identity.ts).

Acceptance:

- Two humans persist as distinct native profiles across reconnects; neither acquires owner/admin authority implicitly.
- Provisioning, health, admin maintenance, Paperclip, CLI/Tauri nodes, pairing and credential refresh still work.
- Cross-owner requests and forged identities fail; organization removal revokes open connections and cached authorization.
- Legacy private history does not become discoverable through lists, direct history, subscriptions, search or event streams.
- The native privacy/creator migration is proven before enabling company-room discovery. If a private-room policy extension is needed, its authorization tests are mandatory, not a future cleanup.

### F1 — Shared founder room, presence and mentions

Use OpenClaw's sharing record as the authoritative native participation record; Clerk membership is an outer limit. Lightbulb stores routing/presentation and Paperclip links, not a second independently editable copy of the same membership list.

Build:

- A session-aware shared protocol carrying qualified author, creator, current owner, full session identity, parent lineage, run identity and capabilities.
- Session-aware panels and deep links: two chats with Friday must not collide as the same agent-only panel.
- Clearly labeled company-readable rooms, participant controls, owner assignment, typing/presence, mention picker and access-aware notifications.
- Authorized subscriptions and canonical-history recovery on reconnect. Never fan out the service-admin event stream indiscriminately.
- Queue/steer/stop, questions and approvals for eligible actors. Use upstream idempotency and expected-leaf guards where applicable; define concurrent-send behavior explicitly.
- Compatibility for old clients; capabilities must hide unsupported actions on web, Tauri and Expo.

Relevant native methods include session.visibility.set, session.members.list/listEvidence/add/remove, session.typing, sessions.assignOwner, sessions.messages.subscribe, users.mentionable and mentions.list/dismiss. Parameter names differ: some methods use key and others sessionKey. Bind to the tagged schemas rather than guessing. [Sharing schema](https://github.com/openclaw/openclaw/blob/v2026.9.3/packages/gateway-protocol/src/schema/sessions-sharing.ts).

Acceptance: two trusted people collaborate without swapped authors, duplicate turns or mixed transcripts; a third organization cannot access the room; participation changes take effect on live connections. A removed participant may remain a reader of a company-readable room—UI and tests must reflect that policy. Mentions do not grant access. Personal history stays private.

### F2 — Visible specialist handoffs

Extend existing tool/run renderers and Paperclip task panels with requesting room, specialist identity, accepted/running/yielded/terminal status, inspect/stop/retry and one delivered result.

Preserve the distinction between ordinary subagent announcement, collector results and ACP/session contracts. A dispatched run being accepted is not proof its outcome reached the parent. Upstream still identifies gaps in channel-visible progress across yield; build only against demonstrated events. [Yield contract](https://github.com/openclaw/openclaw/blob/v2026.9.3/docs/concepts/subagent-yield-handoff.md).

Keep Paperclip as durable work ownership and follow-up. Do not invent unsupported APIs or assume a one-shot monitor is an indefinitely armed watchdog. Existing adapter limitations around agent-JWT-only operations and experimental objects need explicit compatibility decisions.

Acceptance: Friday delegates; the parent yields; a specialist completes after browser disconnect or gateway restart; the result arrives once in the correct room with its task/run link. Failure and cancellation leave an actionable state.

### F3 — Persistent side conversations

Add a session tree/rail and durable panels for visible children. Link parent and child without copying private parent context indiscriminately. Persist session routing and workspace layout, not just an agent ID.

Let users inspect and continue persistent sessions after the first run ends. Temporary subagent runs stay distinguishable and view-only where upstream requires it. Ephemeral btw conversations are not a substitute for durable child sessions.

Acceptance: two simultaneous conversations with one agent survive reload, back/forward, mobile deep links and task replacement; continuing one does not send to its sibling or parent. Existing personal chat URLs/history remain compatible.

### F4 — Authorized device tools, then full session placement

First expose scoped use of already paired Macs/nodes. Reuse the current device registry and intentional CLI/Tauri node roles.

A critical routing decision: current execNode selection is persisted on the session. Two people in a room cannot safely race to repin it. Start with an explicit stable room binding plus an authorized change workflow, or implement a proven per-run binding before presenting a per-message device picker. Preserve manual browser routing to avoid using another member's Chrome.

Then add full worker-hosted sessions only after worker WebSocket ingress, supported node runtime, placement lifecycle and recovery are proven. Verify the required worker route deliberately; existing browser-view ingress is not evidence of worker support.

Acceptance: a request authorized for founder Mac never executes on another person's machine; offline/unavailable devices fail clearly; reconnect or placement changes do not replay consequential work. Credentials and approvals remain bound to the correct actor/device/account.

### F5 — Bounded Swarm and Code Mode

Enable these deliberately for the founder, not through new upstream defaults.

Start with shallow, low-fanout representative tasks; choose actual limits from measured task memory, provider capacity and cost. Carry admission/budget controls through child runs and nested tools. Render progress, collected results, partial failure and stop-all behavior. Verify Code Mode tool traces, approvals and history with the configured providers.

Swarm collectors are not proof of persistent agent federation or cloud placement for every child. Do not build the product around an unshipped combination.

Acceptance: compare one realistic task with and without fanout for useful outcome, latency and spend; enforce depth/concurrency and cost bounds; cancellation settles children; results are attributed and delivered once. If nested spend bypasses current admission, fix it before enabling the feature.

### F6 — Personal provider accounts and routing

With F0 identity proven, add member-bound account connection/management and supported account order/selection using upstream APIs.

UI must show account owner, selected account, default and allowed fallback. Define who pays before a run starts. A room account choice is session-sticky; another human steering the room must not silently change the payer. Preserve Lightbulb entitlements and spend visibility, including BYO-account attribution where exact provider billing is unavailable.

Keep secrets out of transcripts, logs and notification payloads. Test connection cancel/retry, provider expiry, member removal, reconnect and simultaneous account flows.

Acceptance: credentials attach to the correct durable profile; another member cannot select an unauthorized account; observed routing matches the displayed payer or produces an explicit fallback/error. Do not claim the account is inaccessible to a trusted runtime administrator or unrestricted shell.

### F7 — Adjacent features, shipped behind separate gates

These do not all need to wait for Swarm. Deliver independent slices after their prerequisites pass:

| Feature | Deliverable | Acceptance gate |
|---|---|---|
| Workshop UX | Extend existing skills/proposal review surfaces; show agent ownership and migration/review state | R1 storage survival tests plus approve/update/reject/restore through Lightbulb |
| Public transcript links | Authenticated publication action and narrow public read route/renderer using native grant semantics | Owners only publish; expectedSessionId enforced; tools/reasoning/files excluded; future-text warning; revocation and caches tested |
| Slack stop/title | Update app manifest subscriptions and channel/session mapping | Native stop/title events reach the correct Lightbulb session without duplicate or cross-user action |
| Model/fallback controls | Integrate supported account/model choices without bypassing policy | Expired/unsupported route and fallback behavior visible; no unmetered browser RPC |
| Usage/title/recovery UI | Expose verified events and cost-aware utility actions | Live/history parity; disconnected status is not falsely shown as idle |
| Diagrams/live browser | Reuse our renderers and browser panel where compatible; add missing UI explicitly | Safe diagram rendering, correct session ownership and reconnect behavior |
| Meeting library | Inventory actual runtime/plugin dependencies and persistence; build transcript/search/download UI with consent and access control | Captures and archives survive replacement; private recordings/transcripts cannot leak between audiences |
| Team Reports | Install/enable compatible optional plugin, explicit authorized data sources and report UI | Permission-bound ingestion, durable history, bounded summaries/cost and removal behavior |

The current gateway Control UI and general public gateway HTTP ingress are disabled. An upstream public link is not automatically reachable from Lightbulb. Prove the standalone read surface under that configuration or implement a narrow sanitized renderer; never expose the whole gateway as a shortcut.

### F8 — Confidential rooms and the broader network

Keep this in the roadmap rather than claiming the image has delivered it.

For selective invite-only reading, prefer a narrow extension of the single native sharing policy/store over two competing ACL systems. Cover listing, direct reads, search, subscriptions/events, attachments, approvals, public sharing, child-context inheritance and revocation. Model-facing tools/memory must respect the intended boundary too; where unrestricted execution defeats it, use separate runtime trust boundaries.

For cross-organization collaboration, retain owner-isolated gateways. Add explicit agent/task endpoints with scoped identity, grants, revocation, budgets and audited task/result exchange. Start with one authorized handoff and returned artifact, not broad access to another organization's sessions, tokens, files or devices.

Acceptance: revoke a grant during in-flight work, reject replay/cross-owner substitution, preserve provenance and prevent recursive delegation loops. Design this after the founder's single-organization workflow proves valuable.

## 5. Implementation seams and reviewable PR sequence

These are bounded delivery slices, not a mandate to create a new generic orchestration framework.

| Slice | Main code surfaces | Completion evidence |
|---|---|---|
| A. Fork candidate | agent fork patches, release workflow, Workshop root, plugin compatibility | Patch ledger, fork tests, candidate build |
| B. Mixed-version lifecycle | Backend version/config resolver, schemas, container repository, update/admin/ECS paths | Old/new schema tests, durable founder override, safe staged activation |
| C. Extended image qualification | Dockerfile, image workflow, schema artifacts, local-sqlite smoke/migration harness | Clean/migrated dev boot, final-image digest, restore rehearsal |
| D. Founder rollout | Existing admin flow, release assignment and observability | Exact founder running digest, real tasks, lifecycle survival and work-cycle observation |
| E. Identity/privacy foundation | Gateway pool/operator/node paths, Clerk mapping, native auth/profile policy, legacy sessions | Two-person identity plus machine-path and privacy negative tests |
| F. Shared protocol and rooms | Shared gateway/chat types; session hooks; workspace navigation; backend proxy; mobile | Company room with correct authorship, concurrency, reconnect and client gating |
| G. Handoffs and side sessions | Existing tool/run renderers, session rail, Paperclip task/run mapping | Durable parent/child delivery and independently continuable sessions |
| H. Devices and placement | Node proxy, pairing, Tauri node transport, worker ingress | No wrong-device execution, deterministic binding, recovery |
| I. Swarm/Code Mode | Config capabilities, admission/spend path, run rendering | Bounded fanout, cancellation, useful benchmark |
| J. Accounts and adjacent UX | Account/channel/skills settings, public route, optional feature screens | Per-feature gates above |
| K. Confidential/interorg network | Native policy extension or runtime boundary; scoped task exchange | End-to-end access and revocation evidence |

Shared/client starting points: [gateway types](https://github.com/TheLightbulbCompany/lightbulb/blob/main/packages/shared/src/gateway/types.ts), [session keys](https://github.com/TheLightbulbCompany/lightbulb/blob/main/packages/shared/src/chat/sessionKey.ts), [agent chat hook](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/frontend/src/hooks/useAgentChat.ts), [agent sessions hook](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/frontend/src/hooks/useAgentSessions.ts), [workspace panel registry](https://github.com/TheLightbulbCompany/lightbulb/blob/main/apps/frontend/src/components/workspace/panelRegistry.ts). Extend the corresponding shared history/connection code, ChatPanel and mobile agent-session screens together.

Dependency order: A + B → C → D. E requires a qualified dev runtime and must pass before F. F → G → H → I is the preferred user-facing order. Accounts require E/F but can be developed independently of Swarm. Workshop storage is mandatory in A; its UX and other adjacent slices can ship independently once safe.

Do not estimate the whole project from upstream diff size. First obtain the patch-replay result, Workshop migration test and identity compatibility prototype; those determine the critical path. Estimate and assign the remaining bounded slices from that evidence.

## 6. Founder definition of done

Runtime acceptance and full-feature acceptance are separate milestones.

The upgraded founder runtime is ready when its release is verified live, config/plugins work, state survives replacements, its assignment survives lifecycle changes, and rollback has been rehearsed. This can ship before all new screens.

The founder multiplayer pilot is ready only when this complete journey works:

1. Founder and a second trusted colleague enter a clearly labeled company room while their personal histories remain unexposed.
2. Friday receives correctly attributed messages and delegates to a durable specialist and a temporary worker.
3. A persistent side conversation is opened, continued and restored independently.
4. Device work executes on the deliberately selected authorized Mac and an eligible human resolves an approval.
5. The browser disconnects, the gateway is replaced, and session/task state returns without duplicate consequential work.
6. The specialist result appears once in the requesting room with its Paperclip issue/run lineage.
7. A bounded Swarm task respects limits, can be stopped, and produces a useful attributable result.
8. Personal account routing matches the declared account/payer; no credential enters chat history.
9. A Workshop skill survives replacement, and a public transcript can be explicitly published then revoked.
10. Removing organization access closes live access; participation-only removal respects company-readable semantics; cross-organization and legacy-private negative tests pass.

Mobile and desktop must either support each enabled feature correctly or clearly gate it; a new web control must not corrupt an older client's session routing.

Only after founder acceptance should a separate broader rollout promote the fleet default and enumerate both running and stopped containers. New-user default promotion is a distinct action from migrating existing owners.

## 7. Assessment of the earlier investigation

The earlier work is useful: it located the fork dependencies, runtime/state risks and integration gaps, and it explicitly corrected several initial diagnoses. It is a starting point, not a production-readiness verdict.

This plan updates it in consequential ways:

- Several 8.1 fixes and integration surfaces are now merged; old finding statuses must not be treated as current missing work.
- Much of “9.x multiplayer” was already in 8.1; the upgrade does not automatically enable our UI.
- Protocol shape compatibility is not proof of profile/auth, plugin, configuration or persistence compatibility.
- Founder image application is not yet a persistent canary release assignment.
- A single global schema is insufficient for a mixed 8.1/9.x fleet.
- Target config activation currently risks exposing new config to the old running process.
- Workshop needs a durable-path solution consistent with local SQLite, not an indiscriminate agentDir move.
- Native room membership is not a confidential read ACL.
- Existing image-size/cold-start estimates need current SOCI/runtime measurements.

Treat the older audit's assumptions and withdrawn diagnoses as historical context. Use current source and the acceptance evidence above for release decisions. [8.1 investigation](https://github.com/TheLightbulbCompany/lightbulb/blob/main/docs/superpowers/plans/2026-09-06-openclaw-81-audit-findings.md), [9.x capability investigation](https://github.com/TheLightbulbCompany/lightbulb/blob/main/docs/superpowers/plans/2026-09-08-openclaw-9x-capability-audit.md).
