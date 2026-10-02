# Native Sessions and Lightbulb MCP — Build, Ship, Verify

> Working replacement for the September 30 shared-native-sessions tracker. Delete this plan when the work is accepted; PR history is the durable record.

**Outcome:** A Claude Code or Codex session can be used from its native client, Lightbulb on Mac, web, or phone with clear identity, native controls where actually supported, correct replies and receipts, and explicit approval for a new outbound audience.

**Architecture decision:** Discovery remains the default for independently started sessions. Treat the current discovered-session path as a companion mode, not full native-client parity. Managed launch is paused at the owner's request. A standard remote Lightbulb MCP is a separate account-connected messaging surface; it must not claim to be a particular live coding session. The local Mac bridge remains necessary for exact session attribution, transcript discovery, approvals, and verified live control.

## Source of truth and release boundary

| Work | Current state | Evidence still needed |
| --- | --- | --- |
| Discovery, room mentions and threaded replies, remote starts, offline follow-up queue, native tools and exact owner approval | Shipped in the earlier production backend/web/iPhone OTA and signed Mac 0.4.430 | Physical iPhone and teammate acceptance; full provider/target matrix |
| Native outbound denial and applied receipt | Production backend hotfix 53b129d5; manually verified one denied and one approved Claude send | Repeat on Codex and after a Mac/backend reconnect |
| Pending approval reconnect and old unknown-control retirement | Merged at e55ce778; production [run 36904697072](https://github.com/TheLightbulbCompany/lightbulb/actions/runs/36904697072) succeeded. A pending Claude intent survived a forced Mac reconnect and was denied without a room write on Mac 0.4.430. Signed Mac run 36904702182 remains in flight | Install the new Mac only after its signed release, then test old unknown command recovery and iPhone/teammate paths |
| Metadata-only session analytics | [PR 3523](https://github.com/TheLightbulbCompany/lightbulb/pull/3523) reconciled onto main at 6204e55 (both routes kept); Frontend and Mobile CI green, Backend CI re-run after a runner cancellation | Merge on exact-head green, production promotion, PostHog canary joining start, session, and request IDs |
| Supademo readiness | Draft captures exist; clean video pilot and export unverified | Real 10–15 second product capture, complete playback, chosen visibility, downloaded and opened video export |

Do not infer production from a merge. For each release record the main SHA, workflow conclusion, signed Mac version, iPhone OTA/runtime compatibility, live response, and native or database receipt.

## Authority and guardrails

| Path | Who chooses the action | Authenticated source | Server checks | Approval and receipt |
| --- | --- | --- | --- | --- |
| Room @session request and reply | A permitted room member selects a session; Lightbulb routes the native answer back to that message's thread | Clerk caller and exact session ID; Mac verifies native process and generation | Room membership, session sharing/control, live capability and request ID | Existing request needs no new destination choice; show queued, delivered, rejected, or unknown accurately, then one threaded answer |
| Native session starts a new message | Claude/Codex model may call list_targets and propose send, guided by the installed skill or a user request; it does not get publishing authority from the skill | Local MCP child has no workspace token. Resident Mac checks Unix peer UID/PID, provider process, conversation/turn and live mirror, then mints a short-lived device token | Backend rechecks owner/device/connection/generation, destination membership/grants, expiry, idempotent request ID, and two-hop loop cap | Park exact text and destination privately. Only that session's human owner can allow or deny the matching approval ID and input digest. On allow, commit once and settle receipt; on deny, no room/session write |
| Remote account MCP (shipped) | Model may call ordinary messaging tools after OAuth connection | OAuth token proves a human/workspace and MCP audience, never a native coding session | Scope, workspace and room membership, bounded target, idempotency and audit; never accept model-supplied source actor | Decided 2026-10-02: the consented `lightbulb:messages:write` scope on Clerk's consent screen IS the write approval; no per-send Lightbulb approval card. The message is a human-authored send (author = the signed-in person, “via Lightbulb MCP” provenance), so the native-session “new audience” rule below does not apply to it |
| Phone/web follow-up or control | Authenticated owner or explicitly allowed teammate | Clerk identity plus Mac-verified session/generation/active turn | Recheck grant at admission and delivery; offline queue expires after 24 hours; stale prompts and old turns cannot gain authority | Native provider permission prompts remain separate. Unknown means unknown, not applied; retire a blocking old unknown only after the exact old turn ends |

The native model's choice to call send is not proof that a message should be published. The current skill teaches when to use the tool; it grants no authority. A normal answer to a room @session request is routed by Lightbulb itself, so the model should not also call send and duplicate it. Define and test a clearer outbound-intent policy: explicit user-directed messaging and useful cross-session coordination may propose a new send; a new audience still requires the exact owner decision. Teammate text cannot grant itself owner status, change settings, or answer its own permission prompt.

## Build: UX and integration work still needed

### B1. Standard Lightbulb MCP, separate from the local session bridge

**State 2026-10-02.** Endpoint is live in prod (`/mcp` 401s unauthenticated; protected-resource metadata advertises `user:org:read`, `lightbulb:messages:read`, `lightbulb:messages:write`). Clerk has **no Backend API** for custom scopes, CIMD or default scopes: all of it is Dashboard-only. Dev instance (`up-moth-55.clerk.accounts.dev`) is configured: both custom scopes created and advertised, "Publish CIMD support" on with admission "Pre-registered and previously connected clients", default scopes for dynamic clients = email, profile, `user:org:read`, `lightbulb:messages:read`, `offline_access` (write deliberately NOT default). **Owner-gated, still open:** pre-register the two CIMD clients (Clerk's "Pre-register CIMD client" with the popular-client presets) — Claude Code `https://claude.ai/oauth/claude-code-client-metadata` and Codex `https://chatgpt.com/oauth/codex/client.json` (Codex uses that shared URL because Clerk advertises `authorization_response_iss_parameter_supported`) — with scopes openid, profile, email, offline_access, `user:org:read`, `lightbulb:messages:read`, `lightbulb:messages:write`; then repeat scopes + CIMD + default scopes on the **prod** instance. Claude Code requests every scope the protected-resource metadata lists, so the Clerk consent screen is the explicit write consent; Codex omits `scope` and gets the read-only defaults. Decision (owner, 2026-10-02): no per-send Lightbulb approval card for account-MCP writes; the consented write scope plus "via Lightbulb MCP" provenance is the policy until evidence says otherwise.


Build a Streamable HTTP MCP endpoint with OAuth account consent, public-client PKCE support, protected-resource metadata, token audience checks, read and separately consented write scopes, and revocation. Initial tools: list rooms, read messages, send to an existing room or DM, and check a receipt. Reuse the canonical messaging service and its membership/idempotency checks. The source actor comes only from OAuth; do not accept a session ID as sender input. Offer one-click connection during Lightbulb onboarding and copyable Claude Code/Codex HTTP MCP setup as a power-user route. The Mac app must be unnecessary for ordinary room/DM messaging, while the local bridge remains necessary for exact session identity. Ship this as its own reviewed backend/auth/client project, not as a rewrite of the existing local MCP.

**Accept when:** Claude Code, Codex CLI, and Codex desktop can connect with a signed-in Lightbulb account; reads work with the Mac off; write consent, revocation, wrong-workspace and wrong-audience tokens fail closed; messages show the authenticated human and MCP provenance; no remote token can post as a coding session.

**Accepted on prod 2026-10-02 (real client):** Claude Code 2.1.287 connected to `https://api.flickmeon.ai/mcp` via `claude mcp add --transport http`, authenticated through Clerk's consent page with the CIMD client, and from that connection `find_rooms` (36 rooms), `find_sessions` (READY430, `canSend: true`), `send_message` into the owner's Atlas DM (seq 9, `via: lightbulb_mcp`, author = the signed-in human) and `message_receipt` all worked; prod logged the 401 → protected-resource metadata → authenticated `POST /mcp` 200 sequence. Prod Clerk now: both scopes advertised, CIMD published, admission "Any compatible CIMD client", defaults include read, opaque tokens with audience. Still owed: revocation / wrong-workspace / wrong-audience negative checks, and the Codex CLI path.

**Next build item (owner, 2026-10-02): one MCP entry, not two.** A Mac-app user currently sees `lightbulb_native_sessions` (local bridge: list_targets/send/receipt, Mac-attested) and `lightbulb` (hosted account MCP). Fold the account tools (rooms, read, send-as-me, receipt) into the local bridge, calling the backend with the device token, so the installed app leaves exactly one entry named `lightbulb`; keep the hosted HTTP entry only for people without the app. Backend + desktop change, roughly a day; not part of this release.

### B2. Local bridge onboarding and repair

After “Connect this Mac,” offer explicit “Enable session sharing” consent. Enabling already configures user-scoped Claude Code/Codex MCP, hooks, and the sharing skill; app download or pairing alone does not. Show per-provider “Configured for new sessions / Not installed / Needs attention” based on actual config, with a real Retry setup action that reruns enrollment reassertion. Detect absent CLIs read-only at first launch. Explain that a new native session should be started after setup; an existing session may not hot-load the server. Keep Lightbulb running for discovery and delivery. Do not present manual provider mcp-add as a complete substitute for pairing, hooks, and relay.

**Accept when:** fresh install, later CLI install, config-name collision, disabled Claude hooks, app restart, and unpair/revoke all show truthful status and repair. Verify a real list_targets call in both terminal and desktop-created sessions; a config listing alone is insufficient.

### B3. Name-first @session UX with stable identity

Replace the UUID-bearing visible mention token with a selected recipient chip labeled provider, session title, and owner; show folder/status and a short ID only to disambiguate collisions. Keep the immutable session ID in structured composer state and the existing sessionMentions API field. Persist a display-label snapshot with the room delivery record so renames and ended sessions remain readable. Update web, iPhone, and VoiceOver labels; do not speak a full UUID. The owner send-approval card should also show the resolved room/person/session name with the stable ID only as secondary detail. If inline @Name editing is required, use structured mention spans bound to IDs, never regex matching of names.

**Accept when:** duplicate titles, renames, stale drafts, changed access, ended sessions, and cross-room denial cannot retarget or silently drop a send; room and thread show a readable label plus exact delivery state.

### B4. Native-client parity decision and capability UX

**Spike result 2026-10-02 (doc-only, decided).** Codex app-server has no second-client attach: `thread/resume` makes the caller the thread's new owner, `turn/steer` and `turn/interrupt` are per-turn, and the protocol is silent on concurrent clients. Claude Code's own multi-device path is its Remote Control feature (`claude rc`), not a third-party channel; `--resume` spawns a second writer on the same transcript. So no documented provider path lets Lightbulb adopt an independently started session as a live second client. Decision: companion mode stays, badges stay honest, and unsupported controls hand off to the native client ("run it in your Claude Code or Codex terminal"). No controlled mode is built.


Keep companion mode. Claude's peer inbox accepts text and exact permission decisions but has no live attach to its CLI command parser, so slash text such as /chrome is literal. Codex's daemon socket gives one-shot Stop/Steer only. Map nothing else to host actions; keep the queue-only fallback and truthful capability badges, and show unsupported actions honestly with a provider-native handoff ("run it in your Claude Code or Codex terminal") without implying Lightbulb executed a command.

**Accept when:** every control the UI offers is one the provider actually honoured (exact approvals, Codex Stop/Steer, reconnect, complete owner transcript), and every control it does not offer says so and hands off, on native Mac, web, and phone.

### B5. Inbound message readability without losing provenance

Keep the server-owned owner/teammate identity boundary. Give direct teammate messages a readable server-derived name rather than generic @lightbulb-teammate. Collapse the no-change workspace advisory; keep changed, unknown-coverage, and overlap warnings. A leading slash typed into the current companion composer must be labeled as literal text with an explicit native-command alternative; a natural-language browser request can use existing tools under their permissions. Never strip the owner verification or turn teammate text into owner input to make commands appear to work.

**Accept when:** owner and teammate prompts have correct authority and readable provenance; spoofed owner labels remain inert; Claude and Codex transcripts avoid duplicate boilerplate; /chrome in the native terminal works, while peer text never silently changes configuration; browser actions still require their own permission.

### B6. UX pass on every shipped session surface

Review the Mac, web, and production-connected iPhone screens as one journey: Chats filters and session search; discovery/sharing setup; New session defaults and progress; transcript/readback; queue versus Steer now and Stop turn; offline outbox/cancel; exact permission and outbound approval; owner/teammate access; room thread reply and Open session. Fix overlapping text, raw IDs, ambiguous receipts, dead buttons, and copy that claims an action completed when it was only admitted. Keep model/effort/permission values visible but editable only where a verified provider API applies. Use the same terms and accessible labels across platforms.

**Accept when:** a person can start on one device, find the exact session on another, understand who may act and what happened, and finish the task without reading a protocol marker or guessing whether a message was sent. Check empty, busy, approval, offline, reconnect, denied, expired, and ended states on Mac and iPhone.

## Ship and verify the existing release

1. Production [workflow 36904697072](https://github.com/TheLightbulbCompany/lightbulb/actions/runs/36904697072) from e55ce778 succeeded, including ECS service and Vercel. Keep the backend-before-Mac order because the new receipt protocol adds a settled field.
2. Finish signed desktop [workflow 36904702182](https://github.com/TheLightbulbCompany/lightbulb/actions/runs/36904702182), verify notarization and Gatekeeper, then install that release and verify one live Mac process. Confirm iPhone production OTA and API URL from the installed runtime.
3. Run manual product acceptance against production using the signed Mac app and production-configured iPhone simulator or physical iPhone. Do not run the deleted local integration or D2E suites. Save short receipts: native transcript item, Lightbulb UI state, backend durable result, and room/thread message.
4. Test: default Claude Code terminal and Codex desktop discovery; phone/web starts with model, effort and permission choices; idle/busy/offline follow-ups and cancel; room @session to each provider and exactly one threaded answer; owner native send denied then approved to room/DM/session; pending send across Mac reconnect; expired/stale/duplicate send; teammate Ask to join/grant/revoke and cross-room denial; fresh Codex Stop/Steer and old unknown recovery; Claude queue-only limitation; iPhone permission cards; app restart and simultaneous sessions.
5. Promote metadata-only analytics [PR 3523](https://github.com/TheLightbulbCompany/lightbulb/pull/3523) only after rechecking its final diff on current main. Query production PostHog for start-to-session, message-to-receipt-to-reply, native approval outcome, offline reconnect, and owner/teammate events using opaque IDs. If a join is absent, call it an instrumentation gap.
6. Make one clean 10–15 second real Lightbulb Supademo pilot after the core journey passes: save, play end-to-end, set intended visibility, download and open a video export. Replace rejected Slack Import copy before recording a public demo.

## Open owner-gated steps (2026-10-02)

- Install signed Mac 0.4.448 (DMG mounted at `/Volumes/Lightbulb 2`); the auto-mode classifier refuses to replace `/Applications/Lightbulb.app`. Then re-install the build that [PR 3563](https://github.com/TheLightbulbCompany/lightbulb/pull/3563) produces.
- Clerk: pre-register the two CIMD clients in dev, then repeat scopes + CIMD + default scopes + pre-registration in prod (see B1). The classifier refuses both the scope grant to a client and any prod Clerk change.
- In flight: [PR 3563](https://github.com/TheLightbulbCompany/lightbulb/pull/3563) (desktop: advisory only when it says something, readable teammate fallback, literal-slash and room-reply notes, quoted spoofed owner labels, per-provider setup status + Retry) and [PR 3564](https://github.com/TheLightbulbCompany/lightbulb/pull/3564) (approval-card destination labels, picker collision details, stale-handle guard, title fallback).

## Decisions to confirm during UX review

- Default teammate control remains owner-only with Ask to join or explicit owner toggle; workspace reading is shared by default. Do not infer send authority from visibility.
- Decided 2026-10-02: remote account-MCP writes need the explicit, separately listed `lightbulb:messages:write` scope and Clerk's consent screen, and no per-send Lightbulb approval. Revisit only with evidence of abuse.
- Decided 2026-10-02: no opt-in controlled mode; companion mode with a clear native-client handoff is the product. Do not market companion mode as full parity.
