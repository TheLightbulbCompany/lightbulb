# OpenClaw 8.1 → 9.3 — capability audit

**Written 2026-09-08.** Working doc. Per CLAUDE.md, DELETE it once the decisions it
drives have shipped — git history and the PR descriptions are the durable record.

We run **`v2026.8.1-isol8.3`** (`openclaw-version.json`, dev+prod both
`2026.8.1-isol8.3-a60bc9e`). Upstream's newest tag is **`v2026.9.3`**. In between:
`v2026.8.2`, `v2026.9.1`, `v2026.9.2`, `v2026.9.3` — **5,122 commits**.

Every citation below is `git show <tag>:<path>` against `~/dev/agent-fork`, never its
working tree (which is a stale 2026.7.2 and reflects nothing). Line numbers are at
`v2026.9.3` unless a tag is named.

Prior audit of 7.2 → 8.1: `docs/superpowers/plans/2026-09-06-openclaw-81-audit-findings.md`
(on branch `docs/deploy-concurrency-trap`). Nothing already reported, shipped or
**retracted** there is repeated here. Its retractions stand.

---

## The one-paragraph answer

**9.x is not a multiplayer release for us, and the jump is cheap on the wire and
expensive on the fork.** The gateway protocol is still 4, the connect handshake is
byte-identical, **zero** RPC methods were removed or renamed, and every one of the 58
methods we call still exists — so our pool, our proxy and our event transform survive
untouched. What 9.x actually did to us is flip four defaults we deliberately left
unset, two of which open one org member's transcripts to another member's agent and
two of which multiply model spend under a $3/day cap — plus three things no changelog
would surface: the `gateway` tool gained a **self-update action** that our own
`operator.admin` scope unlocks, Workshop-applied skills **relocate onto ephemeral
disk** via an automatic startup migration, and the base image **grew 675 MB
compressed**. The genuine new capability is
**per-person model accounts** — which is the thing that has been on our wishlist — and
it still needs the per-member gateway identity we do not have and cannot mint over
token auth. The real cost of the upgrade is not config: it is that **all 16 of our
carried fork patches sit in files upstream rewrote**, and two of them add config keys
to `.strict()` schemas, so a 9.3 base that drops them freezes every container's
config silently.

## One thing to fix now, whatever you decide about the upgrade

**Every `env.vars` write restarts the container's gateway, and we do one write per
variable.** `env` has no reload rule at either tag, so it falls through to the default
`restart` (`src/gateway/config-reload-plan.ts:400,554`; 8.1 `:295`), and
`apps/backend/core/services/provisioning_landing.py:79,145` patches
`["env","vars",<name>]` **one locked read-modify-write at a time**. Provisioning N env
vars therefore fires N full gateway restarts, each with a drain window, on a container
that reports healthy throughout. That is the most likely explanation for the
`/lightbulb` → `GatewayDrainingError` we have been attributing to an upstream bug (§7c),
and it is fixable today with no base bump. Both halves verified independently.

---

# 1. What we could actually use, ranked by value to an owner

| # | capability | what the owner gets | verdict |
|---|---|---|---|
| 1 | **`cron.skipMissedJobs: true`** — new key, `zod-schema.root-shape.ts:340`, read at `src/cron/service/timer-catchup.ts:99` | A container that restarts (image roll, spot replacement, OOM, reprovision, trial wake) stops replaying every missed schedule slot as a real agent run. A trial owner's first run already burns ~42% of the daily cap; a Monday wake currently replays N nightly-CEO slots back to back. One-shot `at` jobs still catch up. | **ADOPT.** One config key. Highest value per byte in the release. |
| 2 | **Per-person model accounts** (#134970 / #132451) — `src/agents/auth-profiles/personal-profiles.ts`, `src/state/user-model-accounts.ts`, RPCs `users.listModelAccounts` / `users.selectModelAccount` / `users.linkAuthProfile` / `users.authConnect.*` (`src/gateway/methods/core-descriptors.ts:155-163`) | Each org member signs in to their own Anthropic/OpenAI/ChatGPT account; their new chats bill their account. | **BLOCKED on identity** — see §3. There is a **partial, proxy-supplied path** that works without identity; also §3. |
| 3 | **Prompt-cache continuity** — 11 PRs, incl. `c27999bcfc3` "avoid pruning warm caches during tool loops" (#140713), `f0cc57d6b46` "preserve cached history when background work changes" (#140799), `331d6b7f686` "keep prompt cache checkpoints on conversation history" (#140797, Bedrock) | Straight reduction in input-token spend on every multi-tool turn, on the provider we bill through. | **FREE on bump.** No config. See the "checked and fine" note about our AIPs below. |
| 4 | **`mcp.sessionIdleTtlMs`** — `zod-schema.root-support.ts:455` (`z.number().finite().min(0).optional()`); resolver `src/agents/agent-bundle-mcp-runtime-shared.ts:19-22` ("idle eviction is opt-in; zero retains the session lifetime") | Idle MCP stdio children get evicted. Today the only bound is `SESSION_MCP_MAX_LIVE_RUNTIMES = 256` (`:16`) — i.e. no bound at 4 GB, on a box that already crash-looped once at 11 agents. | **ADOPT, but only on 9.3.** Note the history: at 8.1 this key was **retired and dead** (`v2026.8.1:src/config/dead-config-keys.test.ts:163`, and listed in the doctor's retired set at `legacy-config-migrations.runtime.retired-media.ts:193`), so **setting it today would fail strict validation and freeze the config.** 9.x reinstated it. Leases and run-owned runtimes are never evicted, so it is safe once live. |
| 5 | **`sessions.title.prepare`** (`core-descriptors.ts:627`, `operator.write`) — documented at `docs/gateway/protocol/rpc-methods.md:182` | The server-side answer to the prior audit's **S3** ("sessions are named after the first user message"): returns `{title}` from the agent's utility model, creates nothing, and the client passes it as `sessions.create.displayName`. Returns `null` for incognito/empty/slash input; never falls back to the primary model. | **ADOPT with a decision.** It is a real utility-model call. It is **not** in `_RUN_DISPATCH_METHODS`, so our proxy would forward it and a browser could trigger small inferences off the typed chat path. Spend is still fenced by the bedrock-gate, so this is a metering-visibility choice, not a spend hole — make it deliberately. |
| 6 | **`ssrfPolicy.blockedHostnames`** — new on `tools.web.fetch` (`zod-schema.agent-runtime.ts:443`), `browser` (`zod-schema.root-shape.ts:159`) and `cron.webhookSsrfPolicy` (`:347`); consumed `src/infra/net/ssrf.ts:381-387` | First mechanism that denies a **publicly-resolving** host, evaluated before the allowlist and before DNS. Fences a container off from `api.flickmeon.ai`, the bedrock-gate function URL, the Paperclip BFF host, `169.254.170.2`. | **ADOPT.** Note `*.example.com` matches subdomains only; the apex needs its own entry. |
| 7 | **Hot-apply widened** — `src/gateway/config-reload-plan.ts:136,230` (8.1: `:90,157`) | Paths *we write* that moved restart → hot: `gateway.auth.token` (credential rotation, `AUTH_CREDENTIAL_PATHS:92`), `gateway.controlUi.enabled` (`:155`), `gateway.nodes.browser.mode` (`:166`), `gateway.nodes.commands.allow` (`:168`), `gateway.http.endpoints.chatCompletions.enabled` (`:152`), `memory.citations` (`:262`), `update.checkOnStart` (`:270`). | **FREE.** The gateway-token one is the prize: a token rotation stops forcing a container restart. Still **not** hot: `browser.*`, `env.vars`, `logging`, `session`, `bindings`. |
| 8 | **`agents.defaults.cwd` / per-agent `cwd`** — `zod-schema.agent-defaults.ts:85`, `zod-schema.agent-runtime.ts:944` | Splits the run cwd from the bootstrap/memory dir: point `cwd` at task-local NVMe while `AGENTS.md` + memory stay on EFS. Faster tool I/O off NFS, and less run debris inside the `workspace-{id}/` tree that `agent_export` tars for the marketplace. | **WORTH A LOOK.** Help text says a distinct cwd needs an unsandboxed run — *unverified, enforcement site not traced.* |
| 9 | **Code Mode** — `docs/tools/code-mode.md` | Hides every tool schema from the model, exposing only `exec`/`wait` over a QuickJS-WASI sandbox. That is the largest single prompt-token cut available to us, and it lands on exactly the constraint the prior audit's E1/E3 were chasing. 9.3 made it materially more usable: persistent JS environment across fast tool replies, optional TS checks, original-source error locations (#141265, #140812). | **NOT NEW** — it exists at 8.1 and is off by default. Flagged because 9.3 is the first version where it is plausibly usable, and nobody has costed it. Needs a dev run before anyone believes a number. |
| 10 | **`sessions_yield` handoff** — `docs/concepts/subagent-yield-handoff.md` (new page) | An orchestrator (Friday) yields its turn while delegated children keep running; the registry starts a successor turn once they settle, preserving the original requester. Applies equally to interactive and isolated cron runs. | **NOT NEW** (the tool exists at 8.1 — `src/agents/tools/sessions-yield-tool.ts`); 9.x hardened the ownership handoff and documented it. We do not use it. Relevant to Friday's delegation model and to run-liveness UX. |
| 11 | **`claws.monitors`** (`core-descriptors.ts:657`, `operator.admin`) | Structured agent-deletion quiesce/drain. `rpc-methods.md:137` describes it as inspect → quiesce → drain with a deletion journal. | **NARROW.** It is agent-removal plumbing, not health. Named because the method list makes it look like a health feed and it is not. |
| 12 | **Mermaid rendering** (#134913 etc.) | Diagrams render in chat. | **NOT AN UPSTREAM ADOPTION.** It is client-side; our chat surface is ours. Listed so nobody goes looking for a server flag. |

**Slack sits in its own section (§7) because we already ship it.** Two items there
would rank in the top five of this table if they were general: **Block Kit rich
replies** (#138896 — zero runtime code, a bundled skill, no new scope, free on the
bump) and the **HTTP Agent View app-id fix** (#136559 — a live bug where every gateway
restart lost Agent View detection). A third, the **native Stop button** (#136706),
costs two lines in the Slack manifest and is the most visible owner-facing feature in
the release.

**Deliberately not recommended:** public session share links (§2), Beam (§2), Team
Reports (GitHub/Discord activity — duplicates nothing we need and adds a plugin),
meeting transcripts (`transcripts.*` is **audio/voice capture**, `live-audio` /
`live-caption` / `recording-stt` — *not* chat transcripts; do not adopt it expecting
otherwise), `skills.library.*` (per-identity, so inert for us), the update/rehearsal
subsystem (`update.runs.*`, bounded repair — we run a pinned image and must not let a
container update itself), and `gateway.controlUi.experimental.customPlugins`
(`zod-schema.gateway.ts:83`) which **must stay false**: it lets user-installed plugins
run native JS in the Control UI with the operator's gateway permissions, and
marketplace agents ship plugin slices into buyers' containers.

---

# 2. Multiplayer / collaboration — what exists, and whether it works for us

**Short answer: it exists, it is real, and it is a usability layer for a trusted team
— upstream says so itself. On our shared-container model the useful half is inert and
the harmful half is on by default.**

Upstream's own doctrine, unchanged between the tags, `docs/concepts/multi-user.md:15-17`:

> "Everyone who can operate an agent can make it do anything that agent can do.
> Session ownership, visibility in the sidebar, and presence indicators are usability
> features, not security boundaries. **If people must not access each other's
> sessions, tools, credentials, or files, give them separate agents or separate
> gateway/host trust boundaries.**"

That is our case exactly. Our backend stays the only real boundary.

## 2a. The identity premise did not change

`GatewayAuthResult.user` is still set in exactly two places, both non-token:
`src/gateway/auth.ts:502` (trusted-proxy) and `:555` (tailscale). Token auth sets
nothing. The 8.1→9.3 diff of `auth.ts` is 47 lines and touches neither.

**9.2's "Owner profiles" (#136819) is the one thing that looked like it might change
this, and it does not reach us — on a detail the changelog does not mention.**

- The gate `shouldUseGatewayOwnerProfile` (`src/gateway/gateway-owner-profile.ts:4-14`)
  returns true for `role === "operator"` + no `authenticatedUserId` + `authMethod ===
  "token"`. **We satisfy all three.**
- But the caller ANDs in a second condition
  (`src/gateway/server/ws-connection/connect-session.ts:200-202`):
  `ownerProfileExpected = shouldTrackPresence && shouldUseGatewayOwnerProfile(...)`,
  and `shouldTrackPresence = !isEphemeralGatewayClient(connectParams.client)`
  (`:176`). `src/utils/message-channel.ts:52-59` returns **true** (ephemeral) for
  `mode === "backend"`.
- We connect as `mode: "backend"` — `apps/backend/core/gateway/operator_rpc.py:216`,
  constant `apps/backend/core/crypto/operator_device.py:96`.

So `client.authenticatedUserProfile` stays `undefined`, exactly as at 8.1. **And even
if it were populated it would buy nothing**: the same commit walls the owner profile
off from sharing (`src/gateway/session-sharing-policy.ts:155-157`, "Owner attribution
never narrows sharing") and from roles (`src/gateway/operator-role-policy.ts:76-78,
93-95`), and it is **one durable per-GATEWAY row**, not per-connection —
`GATEWAY_OWNER_PROFILE_ID = "gateway-owner"`
(`packages/gateway-protocol/src/schema/user-profile-constants.ts:2`), pinned on
`provider = "gateway.local"`, `subject = "owner"`
(`src/state/user-profiles-owner-migration.ts:75-84`). Every connection resolves to
the same single profile.

Both 8.1 short-circuits survive verbatim: `sharedSecretOperatorOwner`
(`connect-session.ts:197-198`) still makes `gateway.roles` inert for token auth, and
`isGatewayAdmin` (`session-sharing-policy.ts:68-72`) still returns `"admin"` at
`:165-167` before reading anything else. **Every RPC on our shared connection is a
sharing admin for every session in the container.**

**Opening one gateway connection per org member buys nothing.** All connections carry
the same token, device identity and scopes, so on each one `authenticatedUserId` is
undefined and the role resolves to `"admin"`. Per-connection separation is not
something the gateway can see.

**There is exactly one lever that mints per-member identity: `gateway.auth.mode =
"trusted-proxy"`** (`auth.ts:481-502`), which requires `ingressAttribution.kind ===
"trusted-proxy"` (`:492-493`) — the backend inside `gateway.trustedProxies` sending
`x-forwarded-*`. That is the prior audit's **G-F6** (2–4 weeks, mostly re-keying the
owner-keyed pool maps), and `apps/backend/core/containers/config.py:1609-1632`
documents at length why we retired exactly this model in March. **The 9.x release does
not change that calculus.** Nothing new here moves G-F6 up or down the list.

## 2b. Public session transcripts (#139489) — reachable RPC, unservable URL, unguarded mutation

New in 9.3: `session.publicShare.set` (`core-descriptors.ts:656`, `operator.write`),
handler `src/gateway/server-methods/sessions-sharing.ts:377`, params
`{sessionKey, agentId?, expectedSessionId, enabled}`
(`packages/gateway-protocol/src/schema/sessions-sharing.ts:49-53`). It mints an
AES-256-GCM sealed locator whose key is HKDF-derived from the gateway device identity
(`src/gateway/control-ui-public-session-token.ts:14-32,112-120`) with claims
`{v, agentId, sessionKey, sessionId, shareId}` and **no expiry**; revocation is the
`shareId` grant row (`src/config/sessions/session-public-share.ts:11-28`). Content is
user/assistant **text only** — images become `[Image omitted]`
(`src/gateway/control-ui-public-session-render.ts:33`); tool calls, heartbeats and
cross-session injections are dropped (`:43-58,81,93`).

Authorization is `canManageSessionSharing(role)` = `role === "admin" || "owner"`
(`session-sharing-policy.ts:209-211`). **We are always `"admin"`, so the container
cannot make a per-member check.**

**Could an org member publish another member's transcript through our proxy? The RPC
would succeed; the link would 404.** Two independent containments:

1. `src/gateway/server-http.ts:562` rejects the public-session route when
   `!controlUiEnabled`, and we set `gateway.controlUi.enabled = False`
   (`apps/backend/core/containers/config.py:1634-1636`).
2. There is no public route to a container's gateway HTTP port at all. The only
   public raw-WS door is CloudFront → the ALB browser-view listener, path-capped to
   `/api/v1/browser-view/*` (`apps/infra/lib/stacks/network-stack.ts:186-196`) and
   terminating at our **backend**.

**The mutation is still unguarded and should be closed before the bump.** Four new
9.x methods carry a caller-supplied session key and are absent from
`_SESSION_KEY_PARAM_BY_METHOD` (`apps/backend/routers/websocket_chat.py:1396-1408`):
`session.publicShare.set`, `sessions.github.options`, `sessions.github.status`,
`sessions.github.confirm` (all `sessionKey`). Three **pre-existing** gaps in the same
class make them practical, because the attacker needs `expectedSessionId`:
`sessions.describe` (param `key` —
`packages/gateway-protocol/src/schema/sessions.ts:450-452` — returns `sessionId`),
`session.members.list` (result includes `publicShare`,
`schema/sessions-sharing.ts:80-88`) and `sessions.github.publish` (`sessionKey`
optional, `schema/session-github-publication.ts:42-43`).

These are **additions to the in-flight plan**, not new work — fold all seven into
PR B's vendored table in
`docs/superpowers/plans/2026-09-08-rpc-proxy-session-guard.md`. That plan's PR A
(default-deny allowlist) is the durable fix: **47 methods were added between these
tags**, and a per-method blocklist will keep losing this race.

An **agent** can reach the same RPC independently of our proxy — `tools.deny` is a
prompt-surface control, not a fence (see `reference_tools_deny_does_not_fence_exec_plus_openclaw_cli`).
There is no `publicShare` verb in `src/cli` or `src/agents` at 9.3, so it needs a
hand-rolled gateway call rather than a one-liner, but the gateway token is in the
container.

## 2c. Beam links — inert three times over

`/beam/<slug>-<idprefix>` is **not** a capability URL and not a hosted service: there
is no server-side `/beam` route, it falls through to the Control UI SPA
(`src/gateway/control-ui-routing.ts:112,124`), and the transcript behind it is fetched
over the authenticated gateway connection (`sessions.catalog.list` / `.read`, both
`operator.read`, `core-descriptors.ts:461-462`). Plugin off by default (unchanged
between tags), Control UI off, no public route. **No action.**

## 2d. @-mentioning teammates (#135853) — exists, hard-fails for us

"Authorized people" resolves against the `user_profiles` SQLite table
(`src/gateway/human-mention-policy.ts:259-261` → `src/state/user-profile-list.ts:29,39`),
and returns FORBIDDEN without a verified profile (`human-mention-policy.ts:111-121`:
*"Human mentions require a verified user profile. Sign in to use mentions."*). That
gate fronts `users.mentionable`, `mentions.list` and `mentions.dismiss`
(`core-descriptors.ts:628-631`). Sending is separately dead
(`src/gateway/server-methods/chat-user-turn-recorder.ts:55,160`). It is a **human
composer feature only** — synthetic clients are rejected (`human-mention-policy.ts:105`)
and no agent tool exists. Delivery is gateway SQLite + a `mentions.changed` event +
Web Push; no email or Slack fanout. **Inert for us; nothing our proxy can supply.**

## 2e. Presence, participants, typing, hovercards — still inert, same root cause

A `mode: "backend"` client gets `presenceKey = undefined`
(`connect-session.ts:181-185`) and produces **no presence row at all** (the only
connect-time `upsertPresence` is inside `if (presenceKey)`, `:595-613`). 9.3 did
improve presence for profile-only identities
(`src/gateway/authenticated-presence-user.ts:15-18`,
`src/gateway/server/client-presence.ts:30`,
`src/gateway/server/ws-connection.ts:612-619`) but all of it is downstream of
`presenceKey`. `presence-projection.ts`, `session-viewer-presence.ts` and
`session-typing-state.ts` are byte-identical between the tags. Hovercard participants
degrade to `{senderKind: "unknown", id: "gateway-client"}` — the same pseudo-person
for every human behind the connection (`src/gateway/session-input-participant.ts:21-35`).

## 2f. ⚠️ The harmful half — cross-session visibility is now open by default

**This is the single most important finding in the audit and it is a regression, not a
capability.** Two default flips, both landing on us because we set neither key:

| | 8.1 | 9.3 |
|---|---|---|
| `tools.sessions.visibility` fallback | `return "tree"` — `src/plugin-sdk/session-visibility.ts:115` | **`return "all"`** — `session-visibility.ts:111` |
| `tools.agentToAgent.enabled` | `routingA2A?.enabled === true` — `session-visibility.ts:206` | **`routingA2A?.enabled !== false`** — `session-visibility.ts:202` |

(Two hops: #133469 in 8.2 took `tree` → `agent`; #136755 in 9.2 took `agent` → `all`
and flipped a2a. Both verified in code at both tags, independently of the changelog.)

`apps/backend/core/containers/config.py:1895-1897` says, verbatim:

> "cross-agent *session* viewing (tools.sessions.visibility / tools.agentToAgent) is a
> separate gate we **deliberately leave at its default**."

That comment was written when the default was safe.

Our session keys are `agent:{agent_id}:{user_id}[:{uuid}]`
(`apps/backend/routers/websocket_chat.py:82-98`). Two org members chatting with Friday
produce `agent:friday:userA:…` and `agent:friday:userB:…` — **same agent id, different
user segment**, i.e. the same-agent branch of
`createSessionVisibilityDecisionChecker`. At `src/plugin-sdk/session-visibility-internal.ts:197-211`:
`"self"` denies, `"tree"` denies unless requester-owned, and **`"all"`/`"agent"` fall
through to `return { allowed: true }` at `:211`.**

So after a bump, `sessions_list` / `sessions_history` / `sessions_search` /
`sessions_send` executed by Friday reach member B's private Friday transcripts, and
with a2a on, Atlas's and every marketplace-deployed agent's too. Nothing of ours stops
it: our tool policy is `allow: ["*"]` and `sessions_*` is not in `TOOL_DENY_BASE`
(`config.py:2001-2006`, `:287-290`); the sandbox clamp only applies when
`params.sandboxed` (`session-visibility.ts:114-125`) and we set no sandbox; and **our
proxy cannot help** — these are agent tools executing inside the container and never
traverse it.

Upstream ships a security-audit check for exactly this shape —
`security.trust_model.cross_agent_session_access_default`,
`src/security/audit-extra.summary.ts:172-253`, whose own wording at `:246-248` is
*"Agents that can reach other agents' sessions, **including other users'
transcripts**."* Remediation is one line of config (`:251`).

**Fix: set `tools.sessions.visibility` explicitly in `write_openclaw_config`
(`"tree"` restores 8.1 exactly; `"self"` is tighter) and set
`tools.agentToAgent.enabled: false` or an explicit `allow` list if Friday↔Atlas
delegation depends on it. This must land in the same change as the base bump, not
after it.**

---

# 3. The auth-profiles subsystem — what it is, and whether it unblocks us

## First, a correction to the premise

**`src/agents/auth-profiles/` is not new.** It exists at `v2026.8.1` with 40+ modules.
A file-level diff makes it look new because eight non-test files were *added* inside
it: `credential-schema.ts`, `external-auth-runtime.ts`, `mutation-lineage.ts`,
`personal-profiles.ts`, `profile-ids.ts`, `profile-usage-stats.ts`,
`selection-error.ts`, `store-runtime.ts`.

Same correction for the other leads: **`agent-bundle-mcp-*` is not new** — 26 files of
it exist at 8.1 (`git ls-tree v2026.8.1 -- src/agents/`); 9.3 adds five, four of them
tests. Genuinely new and non-test: `agent-tool-availability.ts` (81 lines — a
`WeakMap` binding tool-availability metadata onto tool objects; internal plumbing),
`agent-delete-databases.ts` (120 lines — planning safe SQLite deletion when an agent is
removed; the OpenClaw-side counterpart to the agent-delete FK sweep our Paperclip fork
already carries), and `assistant-error-transcript.ts` (123 lines — holds a failed
attempt's partial assistant text and commits it only if recovery fails, so a recovered
reply supersedes stray partial text). None of the three is owner-facing.

## What the subsystem is for

Auth profiles are **named credentials for model providers**, with eligibility
ordering, cooldowns and failover (`src/agents/auth-profiles/order.ts:1-5`,
`profile-usage-stats.ts:12-31` — failure reasons `rate_limit`, `billing`,
`auth_permanent`, `overloaded`, `subscription_limit`, with `disabledUntil` /
`blockedUntil` / `cooldownUntil` windows). That machinery existed at 8.1.

**What 9.x added is the `personal:` variant — genuine per-user credential separation
inside one gateway.** A personal profile id is `personal:<ownerProfileId>:<uuid>`
(`src/state/user-model-account-id.ts:4-6`), and the credential is stored in the secret
store under **`scope_kind = "identity"`, `scope_id = <user profile id>`**
(`src/state/user-model-accounts.ts:100-116`). That is new: the prior audit recorded
`normalizeScope()` hardcoding `{scopeKind:"team", scopeId:""}` at 8.1.

Ownership is enforced at selection: a fresh `personal:` selection is refused unless the
requester owns it (`src/auto-reply/reply/directive-handling.auth-profile.ts:21-28`,
"Select a personal model account connected to your signed-in profile"), and the
requester comes from `resolveAuthenticatedProfileId(client)`
(`src/gateway/server-methods/users-profile-access.ts:7-26`), which reads
`client.authenticatedUserProfile` or `client.authenticatedUserId`.

## Is it the thing that unblocks org-member credential separation?

**Yes in design, no in practice — and upstream says so in the same paragraph.**
`docs/concepts/multi-user.md`:

> "**Single-user connections can use the durable Owner profile; every device using
> that profile shares its accounts. To distinguish people on a shared server, use the
> Gateway's identity-bearing endpoint.**"

and, on the same page:

> "This is account-selection convenience inside one trust domain, **not isolation from
> administrators or code running as the Gateway OS user**."

Three consequences for us, each load-bearing:

1. **It needs the identity we cannot mint.** Both `resolveAuthenticatedProfileId`
   inputs are bound at connect and neither is set for token auth (§2a). The
   identity-bearing endpoint is trusted-proxy = **G-F6**, unchanged in cost.
2. **Even with identity, it is not a boundary.** Personal credentials live in
   `state/openclaw.sqlite`; any member who reaches `exec` reads them. This is the
   prior audit's **G-F5** conclusion, now confirmed by upstream's own wording. It
   moves a key from "visible in `env`" to "requires reading a SQLite file".
3. **There is no config surface.** Auth profiles are runtime state + RPC only —
   `authProfile` appears nowhere in `src/config/types.*.ts` except
   `types.mcp.ts:52`. Our generator writes credentials through `models.providers.*`
   and `env.vars`; it cannot declare an auth-profile store. Adopting any of this means
   our backend calling `models auth` / `users.authConnect.*` against each container.

## The one path that DOES work without identity

`docs/concepts/multi-user.md` also records the admin route:

> "Administrators can still create **shared** profiles through the CLI
> (`openclaw models auth login --provider openai --profile-id openai:alice`) and link
> them with `users.linkAuthProfile`… An explicit `/model ...@profile -s` pin outranks
> the link."

and the ownership check is **only built for `personal:` ids** —
`directive-handling.auth-profile.ts:22-27` constructs `validateSelection` inside
`isUserModelAuthProfileId(raw) ? … : undefined`. A **shared** named profile can be
pinned per-session by any caller.

The session pin is durable and sticky: `authProfileOverride` +
`authProfileOverrideSource` on the session entry
(`src/gateway/session-create-service.ts:1370-1371`), inherited by forks, and steering
teammates use the session's account.

**So: our proxy already knows which Clerk member owns a session key. It could mint one
named shared auth profile per member and pin each member's sessions to it — per-member
credential routing and per-member attribution, with no gateway identity at all.**

Be honest about what that is and is not:

- It is **routing and attribution**, not isolation. Every profile is readable via
  `exec` by any member. Same ceiling as G-F5.
- It only pays off where owners actually have more than one credential. Today a
  personal owner has one Bedrock AIP, or one ChatGPT OAuth, or one BYO key. It becomes
  interesting for a Teams org where members want to bring their own keys, and for the
  failover story in the next bullet.
- The **failover** is the sweetener: ordered shared profiles for the same provider
  remain candidates when a pinned account fails (`order.ts`, `usage-state.ts`), plus
  9.3's model-fallback picker (#141330) and `models.authOrderSet`
  (`core-descriptors.ts` — new). A BYO-key owner who hits a rate limit could fail over
  instead of erroring.

**Recommendation: not now.** It is a real path and it is worth writing down so nobody
re-derives it, but it buys little until we have multi-credential owners. Revisit with
the same trigger as G-F6 — a second paying multi-seat org.

---

# 4. What is BROKEN or DEPRECATED for us

## 4a. ☠️ Our own two fork-only config keys are the fleet-outage risk

**The config validator is strict and it throws.** `src/config/zod-schema.ts:17` —
`z.strictObject(OpenClawSchemaShape)`. Unknown key → `src/config/io.load.ts:111,128`
→ `src/config/io.invalid-config.ts:52-66` throws `code: "INVALID_CONFIG"`. On hot
reload the running gateway **keeps the old config and only warns** —
`src/gateway/config-reload.ts:1000-1014`:
`log.warn("config reload skipped (invalid config): …"); return;`. That is our known
silent-freeze mode, unchanged at 9.3, and still the only signal. A *restart* on a
rejected file crash-loops.

Two keys our generator emits are **fork patches**, absent from upstream at both tags,
and both live in `.strict()` objects:

| key we emit | emitter | present at | absent at |
|---|---|---|---|
| `mcp.runtimeScope: "shared"` | `config.py:2653` | `v2026.8.1-isol8.3:src/config/zod-schema.root-support.ts:461` | `v2026.9.3:…:453-471` — `McpConfigSchema` is `z.strictObject({sessionIdleTtlMs, servers, apps})` |
| `browser.sessionState.{enabled,intervalSeconds,path}` | `config.py:2375-2381` | `v2026.8.1-isol8.3:src/config/zod-schema.root-shape.ts:212` | `v2026.9.3:…:147-213` — the `browser` strictObject has no `sessionState` |

Both emitters gate on the **deployed fork revision**, so a `v2026.9.3-isol8.N` tag
keeps emitting them. **A 9.3 base that does not rebase both schema patches produces a
config 9.3 rejects wholesale — every container.** On a hot reload that is the silent
freeze; on a boot it is a **crash-loop**.

We already have a guard for the first one, and its docstring says exactly this —
`apps/backend/tests/chaos/test_schema_staleness.py:25-48`:

> "The shared-runtime-scope fork patch adds `mcp.runtimeScope` to the STRICT
> McpConfigSchema… if a re-vendor pulled a base image WITHOUT the fork patch, the
> strict schema would reject that key and **crash-loop every MCP owner on boot**."

**There is no equivalent guard for `browser.sessionState`.** Adding one is ~10 lines
in the same file, it is the cheapest item in this entire audit, and it should land
before anyone starts the rebase — not after.

Upstream removed exactly four keys between the tags and **we emit none of them**:
`skills.workshop.allowSymlinkTargetWrites`, `gateway.controlUi.toolTitles`,
`messages.suppressToolErrors`, and a `wizard.lastRun…localModelLeanAutoModel` field.
No re-typings hit us either — every enum and shape we write still parses at 9.3
(`tools.exec.mode:"full"`, `tools.profile:"full"`, `compaction.mode:"safeguard"`,
`contextPruning.mode:"cache-ttl"`, `session.reset.mode:"idle"`, the `bindings[]` route
shape, `agents.entries.<id>.default`, `agents.defaults.systemAgent`, `env.vars`,
`models.providers.*.api:"bedrock-converse-stream"`, and the whole Slack block).

## 4b. Four default flips that land on keys we deliberately left unset

| key | 8.1 | 9.3 | applied at | what it does to us |
|---|---|---|---|---|
| `agents.defaults.subagents.maxSpawnDepth` | `1` | **`5`** | `src/config/agent-limits.ts:30`; read `src/agents/spawn-plan.ts:294`, `src/agents/subagents/spawn/subagent-capabilities.ts:369` | We emit only `subagents.{allowAgents:["*"], model, runTimeoutSeconds}`. Delegation goes from depth-1 leaves to 5 deep with `maxChildrenPerAgent` still 5 (`agent-limits.ts:26`). Against a $3/day cap with `allowAgents:["*"]` that is a fan-out of up to 5⁵. **(The changelog's key name `maxDepth` is wrong; the key is `maxSpawnDepth`, bounds `.min(1).max(5)`, so 5 is now the schema maximum.)** |
| `tools.swarm.enabled` | `false` | **`true`** | `src/agents/subagents/swarm/swarm-config.ts:15`, applied `:45` | We emit no `tools.swarm`. Collector subagents + `agents_wait` switch on with `maxConcurrent: 8`, `maxChildrenPerGroup: 50`, `maxTotalPerGroup: 200`. **8 concurrent children on 1 vCPU / 4 GB is a reliability problem before it is a cost one** — this is the box that crash-looped at 11 agents in 2026-08. |
| `tools.sessions.visibility` | `"tree"` | **`"all"`** | `src/plugin-sdk/session-visibility.ts:111` | §2f. Cross-member transcript read. |
| `tools.agentToAgent.enabled` | `false` | **`true`** | `src/plugin-sdk/session-visibility.ts:202` | §2f. Empty `allow` = all pairs. |

All four are one-line config fixes and **all four must land with the bump**.

**Two more items in this class live in the image lane and are written up there, not
repeated here:** the `gateway` tool's new `update.run` action, which our
`operator.admin` scope unlocks for every agent (**§6c**), and the Workshop-skills
relocation onto ephemeral disk driven by an automatic startup migration (**§6e**).
Both are the same shape as the four above — a default or a write root that moved under
a decision we had already made — and both need a call before the first 9.3 boot.

## 4c. A one-way state-DB migration — this is what breaks rollback

`skills.workshop.allowSymlinkTargetWrites`'s replacement ships a **state schema
v15→v16 migration that runs at ordinary gateway open, not just under Doctor**:
`src/state/openclaw-state-db-contract.ts:16`, registered
`src/state/openclaw-state-db-maintenance.ts:477-480`, body `:402-462` (rewrites
`record_json`, rebuilds `skill_workshop_collection_reviews`, `ALTER TABLE … DROP
COLUMN`), invoked from `src/state/openclaw-state-db.ts:396-397`.

It is one-way: an 8.1 binary then refuses the DB as `"incompatible"`
(`src/state/openclaw-database-preflight.ts:310-317`).

**A 9.3 → 8.1 rollback needs the pre-upgrade `openclaw.sqlite` restored from the
litestream replica, not just a re-pinned image.** Write that into the rollback plan
before the dev canary, not after.

## 4d. Things that are fine — checked, so nobody re-checks them

- **Protocol is still 4.** `packages/gateway-protocol/src/version.ts:2,4` byte-identical
  at both tags. Our `minProtocol: 4` / `maxProtocol: 4`
  (`operator_rpc.py:210-211`) needs no change.
- **Connect handshake unchanged.** The whole `frames.ts` diff between tags is one
  hunk: `HelloOkSchema` gains optional `controlUiUrl`. `ConnectParamsSchema` is
  byte-identical, including the `device{}` and `auth{}` shapes.
- **`auth.mode: "token"` is more permissive, not less.** `resolveConnectSecret`
  (`src/gateway/auth.ts:145-151`) now accepts the secret in `auth.token` *or*
  `auth.password`. The new weak-token rejection (`known-weak-gateway-secrets.ts:29-31`,
  wired at `auth.ts:162,327`) rejects `""`/`"undefined"`/`"null"` as the configured
  token; `config.py:1538-1539` already raises on an empty token.
- **Zero RPC methods removed or renamed.** 389 → 436 core methods, 47 added, 0
  removed. All 58 method names we reference across `apps/backend`, `apps/frontend`,
  `packages/shared` and `apps/mobile` still resolve. Required params on the methods we
  call are byte-identical; every other change is an added **optional** field
  (`chat.send` +`mentions`/`expectedPermissionMode`/`expectedToolOverrides`,
  `chat.history` +`maxBytes`/`inputRunIds`, `skills.status` +`sessionKey`,
  `sessions.list` +`activeOnly`/`hasBoard`).
- **Chat event stream is wire-compatible.** The `stream` discriminator is the same
  20-value set at both tags; tool phases `start`/`update`/`result` unchanged;
  `ChatEventSchema` union byte-identical (`status`/`delta`/`final`/`aborted`/`error`);
  assistant text is **still block-cumulative** with incremental `delta`, coalesced by
  `mergeAgentTextPayload` (`src/gateway/server-chat.ts:392-401`, renamed from
  `mergeBufferedAgentPayload`, same contract). Two free additions worth consuming:
  `ChatStatusEventSchema.retry{attempt,maxAttempts,reason:"rate_limit"}` and
  `ChatErrorEventSchema.errorDetail{provider,model,failoverReason,httpStatus,…}`
  (`logs-chat.ts:327-334`, `:371-395`) — both feed our error card and the
  `_budget_error_code` reclassification directly.
- **Bedrock prompt caching is NOT silently off for us.** `docs/reference/prompt-caching.md:159`
  warns that opaque application-inference-profile ARNs (which ours are) resolve to no
  cache retention *unless `cacheRetention` is set explicitly* — and we set
  `"cacheRetention": "long"` at `config.py:1745`. Same caveat exists at 8.1
  (`v2026.8.1:docs/reference/prompt-caching.md:113`). **No finding. Recorded because it
  looks like a large one until you check the emitter.**
- **`gateway.cliAgents.enabled` is not a new key** (correcting the changelog): it
  exists at `v2026.8.1:src/config/zod-schema.gateway.ts:104-108` with an identical
  schema; only the resolved default flipped `false` → `true`
  (`src/gateway/control-ui.ts:1101`, `session-catalog-terminal-start.ts:35`,
  `terminal.ts:321,413`, all `=== true` → `!== false`). It gates the Control-UI model
  picker **and** `sessions.catalog.startTerminal`, which spawns a real PTY
  (`terminal.ts:449-465`). Low practical impact — our Control UI is off and
  `gateway.terminal` is unset — but it is a shell surface that switched itself on.
- **`channels.<id>.enabled: false` now hard-disables the plugin**
  (`src/plugins/config-activation-shared.ts:185-193`). No change for us: we only ever
  write `channels.slack.enabled: true` or omit the block.
- **`session.maintenance.maxEntries` default 500 → 5000** — no change, we pin `2000`.
  Overflow is now archived-with-history rather than removed; `maxDiskBytes: "500mb"`
  is still the real bound.
- **`toolsBySender` is not new** (`src/config/types.tools.ts:400,439` at both tags);
  9.x only extracted the implementation into `src/config/tools-by-sender.ts`. We do
  not use it. Given the live Slack product it may be worth a look — per-Slack-sender
  tool policy is one of the few places where real human identity *does* reach the
  container — but it is out of scope for a "what did 9.x add" audit.

---

# 5. Honest upgrade-cost estimate

**This is a project, not a tag bump — but not for the reason anyone expects.** The
wire is free. The config work is a day. **The fork rebase is the whole cost.**

## 5a. The fork rebase is the project

We carry **16 functional patches** over `v2026.8.1` (`git log v2026.8.1..v2026.8.1-isol8.3`)
touching 33 non-test files. Upstream rewrote **every one of those files** between the
tags:

| our patch area | our diff | upstream churn 8.1→9.3 |
|---|---|---|
| `src/gateway/session-utils-store-lookup.ts` | 28 lines | **589** |
| `src/state/openclaw-agent-db.ts` | 24 | **485** |
| `src/agents/embedded-agent-runner/run/tool-result-truncation.ts` | 17 | **551** |
| `src/agents/agent-bundle-mcp-*` (MCP runtime scope) | ~40 | **~1,180** across 4 files |
| `src/gateway/server-methods/chat-history-handler.ts` | 40 | **295** |
| `src/agents/workspace.ts` (native-seed sentinel) | 119 | **233** |
| `src/agents/embedded-agent-runner/run/attempt-setup.ts` | 41 | **256** |
| `extensions/browser/src/browser/chrome.ts` | (session-state launch path) | **170** |
| `src/logging/redact.ts` + `redact-patterns.ts` | 39 | **125** |
| `src/config/zod-schema.*` (2 fork-only keys) | 10 | **~30** + surrounding refactor |

None of our patches were upstreamed. I checked the two most likely: upstream at 9.3
has its own `shellReferencePreservingPatterns` mechanism (`src/logging/redact.ts:46,173,706-716`)
but **not** our `SHELL_EXPANSION_PRESERVING_PATTERN_SOURCES` set for HTTP auth headers
(`v2026.8.1-isol8.3:src/logging/redact-patterns.ts:125-132`), and upstream's
`CHUNK_UNSAFE_PATTERN_SOURCES` still lacks our two `GATEWAY_SECURITY_*` patterns.
`browser.sessionState` and `mcp.runtimeScope` do not exist upstream at all.

**Estimate: 1.5–3 weeks for the rebase alone**, dominated by the MCP-runtime-scope and
browser-session-state patch sets, which sit in the most heavily rewritten code. Each
patch needs its behaviour re-proved, not just re-applied.

Worth asking before spending it: *do we still need all 16?* Several were written
against 7.2-era bugs. Two candidates to drop rather than rebase — `504855fb4d9`
("a failed session-store read is unavailable, not an empty store") and `cb5049e7f5b`
("checkpoint PASSIVE on all agent-DB and state-DB closes") — sit in exactly the files
upstream rewrote most (`session-utils-store-lookup.ts`, `openclaw-agent-db.ts`), and
9.x shipped a large SQLite reclamation/lifecycle rework in the same area. **Unverified
— nobody has checked whether upstream now does what those two patches do.** That check
is the first hour of the project and could remove the two hardest rebases.

## 5b. Everything else, sized

| lane | work | size |
|---|---|---|
| Wire / RPC / events | **none** | 0 |
| Config: pin the four flipped defaults (`maxSpawnDepth`, `tools.swarm.enabled`, `tools.sessions.visibility`, `tools.agentToAgent.enabled`) | `config.py` | half a day |
| Config: adopt `cron.skipMissedJobs`, `mcp.sessionIdleTtlMs`, `blockedHostnames` | `config.py` | half a day |
| Re-vendor the schema (`apps/backend/scripts/vendor_openclaw_schema.sh`) + repin `tests/chaos/_schema` / `PINNED_TAG` | required by `feedback_base_bump_must_repin_vendored_schema` | 1 hour |
| Proxy guard: 4 new + 3 pre-existing key-bearing methods | folded into the in-flight `2026-09-08-rpc-proxy-session-guard.md` | 0 extra |
| Rollback plan: pre-upgrade `openclaw.sqlite` snapshot per container | new, and non-optional (§4c) | half a day |
| Image: Node runtime, plugin SDK, baked plugins, tool catalog | **no source edits** (§6a–6d); npm pin bumps only | 1 hour |
| Image: deny `gateway`; decide the EFS `agentDir` | `config.py`, plus a short design pass on `agentDir` vs local-sqlite/litestream (§6c, §6e) | 1–2 days |
| Image: measure cold-start pull time on the dev canary (+675 MB base, §6g) | canary observation, no code | folded into the canary |
| Slack: bump the plugin pin in lockstep with the base `FROM`; optionally add 2 manifest events | §7 — **no breakage, no new scopes, no re-install** | 1 hour + a manifest edit |
| Dev canary + prod promotion + fleet reroll | the usual: build → `dev.tag` → dev boot check → `prod.tag` → one dispatch → reroll via the admin UI | 2–3 days elapsed |

**Total: ~2–4 weeks, of which ~80% is the fork rebase.** Nothing in the release is urgent enough to justify starting it this week —
but the four default flips mean that **whenever it does happen, the config change is
not optional and cannot be deferred to a follow-up**.

---

# 6. Image lane — Node runtime, plugin SDK, tool catalog, skills

**Headline: the image lane is cheap on code and expensive on two specific things —
a new self-update tool action, and +675 MB of base image.**

## 6a. Node runtime break (#140672) — a non-event for our image

`engines` moves `">=22.22.3 <23 || >=24.15.0 <25 || >=25.9.0"` (`v2026.8.1:package.json:2174-2175`)
→ `">=24.16.0 <25 || >=26.1.0"` (`v2026.9.3:package.json:2269-2270`); `node-version.mjs:5-8`
matches. **But the upstream base image ships the same Node at both tags** — the root
`Dockerfile` ARG at line 17 pins `node:24-bookworm-slim@sha256:3638d9a6…`,
**byte-identical at v2026.8.1 and v2026.9.3**, which resolves to `NODE_VERSION=24.19.0`
and clears both the old floor (24.15.0) and the new one (24.16.0). Our extended
Dockerfile installs no Node (`apps/infra/openclaw/Dockerfile:84` is the `FROM`;
everything after is apt packages, Go binaries and `npm i -g`), so nothing conflicts.
The base still runs `npm install --global npm@latest`, so our three `--allow-scripts=`
layers (`Dockerfile:269,275,282`) remain necessary.

## 6b. Plugin SDK breakage vs our three baked plugins — **zero source edits**

Our plugins import **two subpaths, four symbols, total**:

| plugin | file:line | import | status at 9.3 |
|---|---|---|---|
| `aws-exec-profile` | `plugins/aws-exec-profile/index.ts:26` | `definePluginEntry` from `openclaw/plugin-sdk/plugin-entry` | exists, signature unchanged |
| `paperclip-exec-creds` | `plugins/paperclip-exec-creds/index.ts:26` | same | same |
| `lightbulb-marketplace` | `plugins/lightbulb-marketplace/src/index.ts:16` | `defineToolPlugin` + type `DefinedToolPluginEntry` from `openclaw/plugin-sdk/tool-plugin` | exists; **`src/plugin-sdk/tool-plugin.ts` is byte-identical 8.1→9.3** |

`src/plugin-sdk/plugin-entry.ts:234` (`definePluginEntry`) diffs +9/−1, purely
additive. **Zero plugin-sdk subpaths were removed between the tags** — the export map
goes 321 → 338 keys, 0 removed, 17 added. Every retired subpath named in the changelog
(`config-runtime`, `channel-reply-pipeline`, `infra-runtime`, `channel-lifecycle`,
`channel-message`) is still exported, and its removal window was **pushed from
2026-09-01 to 2026-10-01** with status `removal-pending`
(`src/plugins/compat/plugin-sdk-subpath-records.ts:19-51,99-116`). We import none of
them. `buildChannelTurnMediaPayload` *is* genuinely gone, but it is channel-plugin
surface we never touch.

**One deprecation to watch, not act on:** `openclaw/plugin-sdk/tool-plugin` is recorded
`status: "deprecated"` (`plugin-sdk-subpath-records.ts:202-208`) with **no
`removeAfter` date** — retained until there is a non-executing static-metadata
replacement for `defineToolPlugin`.

`resolve_exec_env` survives intact (`src/plugins/hook-types.ts:141,185,1379-1382`;
`agentId` still on the context at `:303`). **One nuance worth a smoke test:**
`src/agents/bash-tools.exec-request-preparation.ts` flipped agentId precedence from
`params.agentId ?? ctx.hookContext.agentId` (8.1) to `ctx.hookContext.agentId ??
params.agentId` (9.3). Still populated, but our per-agent Paperclip key selection keys
on it.

**Baked-plugin packaging is unaffected.** Capability consent exempts bundled origins
(`src/plugins/capability-consent.ts:153` — `if (!plugin || plugin.origin === "bundled") return;`),
and ours are bundled because they sit under `/app/dist/extensions/`
(`src/plugins/bundled-dir.ts:88`). The manifest `icon` field was deleted (icons now
come from `assets/icon.png`); none of our manifests declare one, and a missing icon
does not block loading. `contracts.tools` is still valid
(`src/plugins/manifest-contract-keys.ts:25`).

Total cost here: bump `lightbulb-marketplace/package.json` `devDependencies.openclaw`
`2026.8.1` → `2026.9.3` (build-time typecheck only; the dir is excluded from the image
by `.dockerignore`). **No source edits.**

## 6c. ☠️ The `gateway` tool gained `update.run` — and our own scope opens the gate

| | 8.1 | 9.3 |
|---|---|---|
| `GATEWAY_ACTIONS` | `["config.get","config.schema.lookup"]` — `v2026.8.1:src/agents/tools/gateway-tool.ts:100`, description *"Writes/restart unavailable; ask human."* (`:116`) | **`["config.get","config.schema.lookup","update.run"]`** — `gateway-tool.ts:107` |

`update.run` performs an **in-container OpenClaw self-update plus gateway restart**.
It is gated on `options.senderIsOwner === true` (`gateway-tool.ts:137`), and
`senderIsOwner` is stamped whenever the caller holds `operator.admin`
(`src/gateway/server-methods/chat-user-turn-recorder.ts:68` →
`hasGatewayAdminScope` → `ADMIN_SCOPE = "operator.admin"`,
`src/gateway/operator-scopes.ts:3`). **Our pooled backend connection claims
`operator.admin` by design** — `apps/backend/core/crypto/operator_device.py:61-84`.

`gateway` is **not** in `TOOL_DENY_BASE`
(`apps/backend/core/containers/config.py:287` — `("canvas","cron","tts","music_generate","video_generate","dashboard")`).

So on the bump, **every agent in every container can self-update OpenClaw out from
under the pinned image tag** and restart the gateway mid-session. This is the same
hazard the prior audit's **H3** described for the `openclaw` CLI on the exec path
(`OPENCLAW_SUPERVISOR_MODE=external`), now reachable through a *first-class tool* the
model can see.

**Fix: add `"gateway"` to `TOOL_DENY_BASE` in the same PR as the bump, or decide to
accept it deliberately.** (`tools.deny` is a prompt-surface control, not a fence — the
exec path is still open — so this is defence in depth, not a boundary. H3 remains the
real fix.)

## 6d. Rest of the tool catalog — clean

`CORE_TOOL_DEFINITIONS` goes 56 → 57. **Zero removed, zero renamed, zero section or
profile changes on existing tools.** The addition is `ls`
(`src/agents/tool-catalog.ts:70-75`, `sectionId: "fs"`) — the *tool* already existed at
8.1 (`src/agents/sessions/tools/ls.ts:99`); 9.3 gives it a catalog identity so it
expands under `group:fs`. Live for us either way under `allow:["*"]`.

Policy families did not rename: `CORE_TOOL_SECTION_ORDER` is unchanged and groups are
still derived as `group:${sectionId}` (`tool-catalog.ts:495-510`), so **nothing we deny
silently un-denies**. The `cron` → `automations` permanent alias still holds
(`src/agents/tool-policy-shared.ts:23`; `automations-tool-name.ts` byte-identical),
so our `"cron"` deny keeps covering `automations`.

Two result-shape breaks for anything parsing tool details — Find/Grep bounded text
moves `details.truncation.content` → `details.content`, and `LsToolDetails` replaces
`truncation`/`entryLimitReached` with `content`/`nextAfter`. Our
`_transform_agent_event` does not parse these, but the **frontend tool-result
renderers should be checked**.

## 6e. ☠️ Workshop skills relocate onto ephemeral disk — silent data loss

At 8.1, applied Workshop skills were written inside the agent's **workspace**
(`v2026.8.1:src/skills/workshop/apply-transition.ts:219-220`, `assertInsideWorkspace`)
— i.e. on EFS. At 9.3 the write root is
`resolveWorkshopSkillsDir(config, agentId)` = `resolveAgentDir(…) + "/workshop-skills"`
(`src/skills/workshop/skills-root.ts:5-11`, enforced by `assertInsideSkillsRoot`,
`apply-transition.ts:216-218`).

We never set `agentDir`, so `resolveAgentDir` falls through to
`resolveStateDir(env)/agents/<id>/agent` (`src/agents/agent-scope-config.ts:609-612`)
= `/home/node/.openclaw/agents/<id>/agent/workshop-skills`. **`~/.openclaw/agents` is
the task-local ephemeral NVMe volume** — our own boot script hard-fails if it is *not*
local disk (`apps/infra/openclaw/local-sqlite/isol8-local-sqlite-boot.sh:92-108`) — and
litestream replicates only `state/openclaw.sqlite` and `agents/**/openclaw-agent.sqlite`.

**So on 9.3, Workshop-applied skills die with the task.** And the migration is one-way
and automatic at startup, not `doctor --fix`-only:
`migrateLegacySkillWorkshopProposals` (`src/commands/doctor-skill-workshop-sqlite.ts:579`)
is wired into `src/infra/state-migrations.doctor.ts:2000`, reached from
`doctor-config-preflight.ts:543` (`autoMigrateLegacyState`), and it relocates skills
*out of* the EFS-backed `<workspace>/skills` and `<workspace>/.agents/skills`
(`doctor-skill-workshop-relocation.ts:34-60`).

This is live for anyone who has approved a proposal: we run
`workshop.autonomous.mode = "propose"` for paid owners
(`apps/backend/core/containers/config.py:2159-2165`).

**Fix before the first 9.3 boot: set a per-agent `agentDir` on an EFS path in
`agents.list[]`.** Note that also moves the agent's SQLite off local NVMe unless it is
scoped carefully — this interacts directly with the local-sqlite/litestream design and
needs its own short design pass, not a one-line config change.

## 6f. Bundled skills — count flat, allowlist mechanism unchanged

51 skills at both tags (`control-ui` added, `session-logs` removed); total `SKILL.md`
bytes 152,912 → 152,054. `skills.allowBundled` is unchanged
(`src/config/types.skills.ts:77`, `zod-schema.root-shape.ts:458`, resolution
`src/skills/loading/config.ts:110`, filter `:113-122`), as are the prompt limits
(`DEFAULT_MAX_SKILLS_IN_PROMPT = 150`, `DEFAULT_MAX_SKILLS_PROMPT_CHARS = 18_000`,
`src/skills/loading/skill-prompt-limits.ts:10-11`).

**Two gotchas for the prior audit's E3/F5 token-cutting plan**, both worth writing into
it now: (a) `allowBundled: []` means **allow everything**, not "block everything"
(`config.ts:114` — `if (!allowlist || allowlist.size === 0) return true`); (b) our own
baked skills land in `/app/skills/{paperclip,lightbulb}` (`Dockerfile:737,751`), so
they are source `openclaw-bundled` and **must be named in the allowlist or we lose
them**.

## 6g. ⚠️ The base image grew 675 MB

Measured against the public `ghcr.io/openclaw/openclaw` amd64 manifests, compressed
layer bytes (the 8.1 and 9.3 rows were re-measured independently against GHCR while
writing this doc and matched exactly):

| tag | total | largest layer (`/app/node_modules`) |
|---|---|---|
| 2026.8.1 | 454 MB | 223 MB |
| 2026.8.2 | 455 MB | 223 MB |
| **2026.9.1** | **1,239 MB** | **989 MB** |
| 2026.9.2 | 1,126 MB | 869 MB |
| 2026.9.3 | 1,129 MB | 870 MB |

All of it lands at 9.1 and **none of it is a new production dependency** (the dep diff
8.2→9.1 is −1 production dep, zero added). The cause is the 9.1 Dockerfile
restructure: `runtime-assets` moved from `FROM build` + `COPY --from=production-deps`
to `FROM production-deps` + `COPY --from=runtime-build-output`, and 9.3 additionally
reorders `postinstall-bundled-plugins.mjs` before the prune so packaged roots keep
plugin-local dependency trees.

Uncompressed growth is **inferred, not measured** — expect roughly 1.5–2.5 GB. Our
extended image targets 2–3 GB against Fargate's 20 GiB ephemeral default, so it still
fits. But **Fargate pulls the whole image on every cold start**, so container
cold-start pull time grows materially, and cold start is on the trial wake path and the
provisioning path (which already runs at p50 167s against a 300s doctor budget).
**Measure the boot time on the dev canary before flipping `prod.tag`.**

Also: `dist` 53.9 → 63.3 MB, `extensions` 8.4 → 14.2 MB, `docs` 11.0 → 13.3 MB;
`libgomp1` added; `pnpm-lock.yaml` now shipped into the runtime image. Prompt size is
flat. Boot time gains one state migration (the Workshop relocation scan) inside the
existing `autoMigrateLegacyState` preflight — it walks every agent's workspace skill
root.

## 6h. Image bump checklist

1. `apps/infra/openclaw/Dockerfile:84` `FROM …:2026.9.3-isol8.N` + `openclaw-version.json#upstream`.
2. All eight baked `@openclaw/*` npm pins `2026.8.1` → `2026.9.3` — **all eight publish
   a 2026.9.3 dist** (verified against the npm registry: slack, discord,
   amazon-bedrock-provider, lobster, parallel-plugin, firecrawl-plugin, acpx,
   tokenjuice).
3. `lightbulb-marketplace/package.json` `devDependencies.openclaw` → `2026.9.3`.
4. Decide on `"gateway"` in `TOOL_DENY_BASE` (§6c).
5. Decide on an EFS `agentDir` before the first 9.3 boot (§6e).
6. No source edits in any of the three plugins.

# 7. Slack lane — 62 commits, and nothing breaks

**Verdict: the bump is safe for the live Slack app, the upside is real and mostly
free, and the lane turned up one live-prod bug that has nothing to do with the
upgrade.** 62 commits, +9,840/−3,849 across 147 files in `extensions/slack/`.

## 7a. Nothing would break the live app

The entire Slack app-config delta between the tags is **two new bot event
subscriptions**, read from the plugin's own canonical manifest builder rather than
prose — the whole diff of `extensions/slack/src/setup-shared.ts` is:

```
@@ -86,6 +86,8 @@ export function buildSlackManifest(botName = "OpenClaw") {
           "app_context_changed",
+          "agent_session_stopped",
+          "agent_session_title_changed",
```

| risk | verdict |
|---|---|
| New OAuth scopes | **None.** `setup-shared.ts:56-78` lists the same 23 bot scopes. Our `len == 23` assertion (`apps/backend/tests/unit/services/test_slack_install_service.py:298-299`) still passes — it compares `oauth_config.scopes.bot`, not events. |
| Changed event subscriptions | +2, additive, opt-in. Ours live in `apps/backend/scripts/slack-app-manifest.yaml`, not in the image. |
| Manifest expectations | None. `setup-core.ts:365` suppresses the generated manifest entirely in HTTP mode; we hand-maintain ours and it already declares `interactivity`. |
| Webhook / signature verification | **Byte-identical** — `extensions/slack/src/http/` has a zero-byte diff, and `@slack/bolt` is pinned `5.0.0` at both tags (`extensions/slack/package.json:11`). |
| Re-install by installed workspaces | **No.** Event subscriptions are app-level and need no new scope. |

**Config shape: every key we emit is still valid.** `SlackAccountSchema` is `.strict()`,
so this was checked exhaustively against `slack_install_service.account_block()` and
`config.py:2670-2690` — `mode`, `webhookPath`, `botToken`, `signingSecret`, `dmPolicy` +
`allowFrom`, `groupPolicy`, `requireMention`, `slashCommand.*`, `enabled`, `accounts`,
`streaming.mode: "progress"`, root `bindings[]`. **Nothing removed, renamed, re-typed or
default-moved.** Retired keys are the `socketMode.*` family, which we do not emit.

**The one real bump cost:** the plugin's `peerDependencies.openclaw` and
`compat.pluginApi` both move to `>=2026.9.3` (`extensions/slack/package.json:26,158`),
so the Slack pin must move in lockstep with the base image `FROM` — it is not a
plugin-line-only bump.

**Visible behaviour changes that are not breakage:** `assistant.threads.setStatus`
became `agents.sessions.setStatus` / `.rename` (`session-status.ts:46,86`), wrapped in
try/catch with a one-shot warning when the subscription is missing (`:49-60`) — it
fails soft, costing two no-op API calls per turn until we add the events. Compact
progress now posts the completion as a **new message** instead of editing the preview
(`monitor/message-handler/dispatch.ts:201-238`), so completions start generating Slack
notifications.

## 7b. `groupPolicy` — unchanged. **Keep our pin.**

Not redundant, not harmful, still load-bearing. The default is still `allowlist` and
still materialized root-only (`src/config/zod-schema.channel-messaging-common.ts:65`,
a **zero-byte diff** between tags), and all three enforcement modules
(`extensions/slack/src/monitor/policy.ts`, `group-policy.ts`,
`monitor/channel-config.ts`) are **byte-identical**. `requireMention`'s `true` is still
a runtime `?? true` read from the resolved account config
(`monitor/channel-config.ts:99`, `monitor/provider.ts:365,629`), so our account-level
pin is honoured. The only change is a refactor collapsing two `allowFrom` refinements
into `refineChannelDmPolicy` (`src/channels/plugins/config-schema.ts:44-73`), same
semantics.

9.3 now *documents* the exact trap we hit — `docs/channels/slack/access-control.md:74`
("with neither scope set, defaults remain `pairing` and `allowlist` respectively") and
`:106` ("all messages in that channel will be **silently blocked**").

**Free diagnostic win worth taking:** #132723 unified every pre-dispatch drop into one
structured log line — `monitor/message-handler/prepare.ts:674-690`, *"Slack inbound
event rejected during preparation"*, with a closed `reason` union (`:359-377`). That
single field would have named our bug in one grep. **Add a CloudWatch filter on it
post-bump.**

## 7c. ☠️ The `GatewayDrainingError` — root cause is almost certainly OURS, and it is live now

No commit between the tags fixes a stuck `restartDraining`; the primitive is
essentially unchanged, with one addition (9.3's reset now aborts the stale drain
controller so anything awaiting the old signal is released,
`src/process/gateway-work-admission.ts:599-602`).

**Why slash commands 503 while DMs are fine** — inference, but tightly constrained by
two read facts. A slash command opens an **isolated** session
(`sessionPrefix: "slack:slash"` → `agent:<agentId>:slack:slash:<userId>`,
`docs/channels/slack/messaging.md:214`), and creating a session goes through
`beginSessionWorkAdmission`, which throws `GatewayDrainingError` the moment admission
is closed (`src/sessions/session-lifecycle-admission.ts:533,642`). A DM **resumes an
existing session** and never passes that door.

**And here is the trigger, which is ours and needs no upgrade to fix.** `env` has **no
reload rule at either tag**, so it falls through to the default `restart`
(`config-reload-plan.ts:400,554`; 8.1 `:295`). Our Stripe-Projects landing writes
`env.vars.*` **one locked read-modify-write per variable** —
`apps/backend/core/services/provisioning_landing.py:79` and `:145`. So provisioning N
env vars fires **N full gateway restarts**, each with a drain window, on a container
that looks healthy the whole time.

Both facts verified independently while writing this doc. **This is actionable today,
independent of the base bump**, and it is the most likely explanation for a drain we
have been attributing to an upstream bug.

What 9.3 *does* improve is the restart **trigger surface**, which is the durable fix:
at 8.1 only `gateway.remote` and `gateway.reload` were exempt and every other
`gateway.*` path was a full restart (`v2026.8.1:config-reload-plan.ts:178`); at 9.3 a
long hot list covers `gateway.http.endpoints`, `gateway.tools`, `gateway.cliAgents`,
`gateway.controlUi.*`, `gateway.nodes.*`, `gateway.terminal`, `gateway.auth.rateLimit`
and `agents.defaults` (`:150-178`) — **desktop pairing writes stop restarting the
gateway** — plus new per-account channel restarts (`restartChannelAccounts`, `:38`,
`:585-593`) so a `channels.slack.accounts.<team>` patch no longer restarts the whole
Slack channel. New diagnosability too: `getActiveGatewayRootWorkHolders()` returns
named origins for every root-work holder (`gateway-work-admission.ts:509-520`). Today
we are blind to *who* is holding a drain open.

## 7d. HTTP-mode gaps — five of six unchanged, two already moot

| gap at 8.1 | at 9.3 |
|---|---|
| `auth:plugin` needs no credential | Still true, byte-identical (`http/plugin-routes.ts:27`; `src/gateway/server/plugins-http/route-auth.ts:37-55` zero-diff). Now *proved* signature-gated end-to-end by a new loopback test asserting `v0=invalid` → 401 (`monitor/provider.transport-credentials.test.ts:158`). |
| relay is a message-only stub | Still true (`monitor/provider-support.ts:128-134`, `relay-source.ts` zero-diff). |
| **no OAuth install flow** | **Still true — definitive.** Zero hits for `oauth.v2.access`, `client_secret`, `redirect_uri`, `/slack/install`, `InstallationStore` or `stateStore` across `extensions/slack/` at 9.3; the `installation*` symbols are Grid install *kind*. `docs/channels/slack/setup.md:23`: "The manifests in this section create a workspace-scoped installation." **Our cold-install rail saves us nothing upstream. Keep building it.** |
| no modals | **The 8.1 note was half wrong.** Inbound `view_submission`/`view_closed` already existed at 8.1 and still do (`monitor/events/interactions.modal.ts:55`, zero-diff). What is absent at *both* tags is `views.open` — the plugin never opens a modal. `trigger_id` is forwarded to plugin handlers, so we can open our own. |
| no uninstall handling | Still true in the plugin (zero hits for `app_uninstalled`/`tokens_revoked`) — **but already closed on our side**: our backend is the webhook front door and revokes the install itself (`apps/backend/routers/slack_app.py:64` `_REVOKE_EVENTS`, `core/services/slack_install_service.py:222`). |
| manifest lacks interactivity | Still true, and irrelevant — suppressed in HTTP mode (`setup-core.ts:365`); ours declares it. |

**The HTTP-mode win that justifies the bump on its own** — #136559,
`monitor/provider.ts:451-465` + `monitor/enterprise-install.ts:265`. At 8.1 an HTTP
workspace install had **no app id at all** (`auth.test` omits `app_id` for bot tokens
and there is no `xapp-` token to derive it from). Three things were silently broken:
the durable Agent View marker never persisted (`monitor/agent-view-state.ts:85-88,117-120`),
so **every gateway restart lost Agent View detection** and the first DM root after a
restart could land on the shared DM session; and the `api_app_id` mismatch guard was
dead code (`monitor/context.ts:578-581`). 9.3 learns the id from the first signed
event, once. **Free on the bump, no config or manifest change.**

## 7e. New Slack capability, ranked by cost

**Free on the bump — no Slack app change, no config change, no reinstall:**

1. **Block Kit rich replies (#138896)** — best value-per-cost in the whole release. The
   commit contains **zero runtime code**: the renderer, charts, tables and buttons all
   shipped at 8.1. What is new is a bundled skill that makes Friday reach for structure
   unprompted (`skills/block-kit/SKILL.md:10`). `docs/channels/slack/rich-messages.md:18`:
   "no additional OAuth scope, file upload, image renderer, or Slack configuration is
   required beyond normal `chat:write`." Gated to Slack-linked owners via
   `requires.config: ["channels.slack"]`.
2. **Session status without the Stop button** — `processing`/`suspended`/`active` plus
   outbound title seeding work the moment the image rolls.
3. **HTTP Agent View surviving gateway restarts** (§7d) — a live bug fix for our shape.
4. **Delivery reliability** — no duplicate replies after a lost Slack ack
   (`streaming.ts:416-428`: only a *definite* rejection replays; ambiguous failures
   retire the buffer); explicit 429s retried against `Retry-After` with `attempts: 3`
   (`client-options.ts:139-200`); unified thread-root selection (`thread-ts.ts:7-21`).
5. **Hot-reload wins** — ack-reaction scope and inbound debounce now apply without
   reconnecting Slack, so no container reroll to change ack behaviour.

**Our config only:** `replyToMode: "first"` in `account_block()` extends status + Stop
to top-level channel @mentions (today they appear only on Agent View DM roots and
channel thread replies, because a reply thread is required —
`docs/channels/slack/messaging.md:119` — and we set no `replyToMode`, default `off`).
Weigh it: non-`off` also changes *where* replies land.

**Manifest edit, expected no re-consent:** **native Stop + inbound title sync
(#136706)** — two lines in `bot_events` on both Slack apps, and the most visible
owner-facing feature of the bump (a real "make it stop" on a long Friday run). No new
scope: the Minimal manifest (`setup.md:156-184`) subscribes to both with 12 scopes
whose only Agent-View-shaped one is `assistant:write`, which we already hold
(*inference, tightly bounded*). **Two things to confirm outside this repo:** that
Slack's config page does not flag a reinstall, and whether editing a publicly
distributed app's manifest re-opens Marketplace review.

**New scope, re-consent by every workspace:** `chat:write.customize` (agents replying
as themselves). Optional at both tags, so **not part of this bump**.

**Not applicable:** Enterprise Grid is not new (all three modules exist at 8.1; the doc
page is a split), needs a separate app, and **loses App Home + Agent/Assistant
lifecycle events** (`enterprise-grid.md:244-246`) — i.e. it would cost us Friday's
Messages tab.

## 7f. Incidental findings on our own Slack manifest

- We carry `features.agent_view` **and** the legacy `assistant_thread_started` /
  `assistant_thread_context_changed` events (`slack-app-manifest.yaml:18`, `:78-79`).
  Per `docs/channels/slack/manifest-and-scopes.md:159` those serve only apps still on
  `features.assistant_view`. Ours are vestigial — and two events a Marketplace reviewer
  will ask you to justify.
- We hold `pins:read`/`pins:write` but subscribe to neither `pin_added` nor
  `pin_removed`, so pin system events never fire. Pre-existing.
- The `assistant_view` → `agent_view` migration is **irreversible** per Slack and forces
  users to hard-refresh. We are already on `agent_view` — **do not let anyone "clean up"
  the manifest back to `assistant_view`.**

---

# What was NOT verified

Stated plainly so nobody treats this doc as more complete than it is.

- **Nothing was run against a live container.** Every claim is read from source at a
  tag. The definitive config check is re-vendoring the schema against a
  `9.3-isol8` tag and running our chaos suite; that has not been done.
- **The config key set was enumerated by *running* our generator** across five provider
  variants (166 key names / ~250 paths) and checking each against 9.3's zod files by
  reading. It was **not** machine-validated by parsing a rendered config with the 9.3
  bundle — that needs a built 9.3 checkout, which the "never mutate the fork working
  tree" rule forbids.
- **Plugin-owned config blocks were not individually validated** against their 9.3
  plugin schemas (`memory-core.dreaming.*`, `memory-wiki.vault*`, `llm-task.default*`,
  `acpx.*`). Unknown *plugin ids* are warn-only
  (`validation-plugin-config.ts:289-301`), so they cannot brick the load — but their
  *keys* can. Separate lane.
- **Only core gateway methods were enumerated** (389→436). Plugin- and node-registered
  methods attach at runtime; a removal there would not appear in the diff. Our one
  exposure is `browser.request` — the name still resolves at 9.3, its param shape was
  not checked.
- **The Control UI / front-end packages are not in the extracted tree**, so the 9.1
  "hovercards show participants" claim is verified at the data layer only.
- **`extensions/` was not extracted**, so Beam's route constant and manifest default
  are doc-sourced, not code-verified. Beam is disabled for us regardless.
- **`agents.entries.*.cwd` sandbox constraint** — documented in help text, enforcement
  site not traced.
- **`canvas.document.view`** (new, `operator.read`) takes a `docId` with no ownership
  binding (`packages/gateway-protocol/src/schema/canvas.ts:5-7`). Minor cross-member
  read surface, not audited further.
- **Uncompressed image growth is inferred, not measured** (§6g). Compressed layer
  bytes were read from the public manifests; the uncompressed figure and the actual
  Fargate cold-start pull time are estimates until the dev canary runs.
- **The frontend tool-result renderers were not checked** against the two result-shape
  breaks in §6d (`details.truncation.content` → `details.content`;
  `LsToolDetails.truncation`/`entryLimitReached` → `content`/`nextAfter`).
- **The `agentDir`-on-EFS fix in §6e was not designed**, only identified. It interacts
  with the local-sqlite boot script and the litestream replication set and needs its
  own pass.
- **Two Slack claims are inference, flagged as such in §7**: that the slash-command
  path fails where DMs succeed because a slash opens an isolated session and passes
  `beginSessionWorkAdmission` (`src/sessions/session-lifecycle-admission.ts:533,642`)
  while a DM resumes an existing one — tightly constrained by two read facts, but not
  reproduced; and that the two new `agent_session_*` events need no scope beyond
  `assistant:write`, read off the Minimal manifest rather than tested. **Whether
  editing a publicly distributed Slack app's manifest re-opens Marketplace review is
  a question for Slack, not for this repo.**
- **Whether any of our 16 fork patches is now redundant upstream** — checked for the
  two redaction patches (still needed) and the two fork-only config keys (still
  absent). The other twelve were not checked. §5a names the two most likely
  candidates.
