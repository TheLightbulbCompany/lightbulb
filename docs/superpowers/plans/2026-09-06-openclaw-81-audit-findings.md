# OpenClaw 8.1 audit — findings and intended changes

**Started 2026-09-06.** Working doc for in-flight work. Per CLAUDE.md, DELETE this
file once the work ships — git history and the PR descriptions are the durable record.

We run the Isol8 fork at tag **`v2026.8.1-isol8.3`** (`openclaw-version.json`,
dev+prod both `2026.8.1-isol8.3-8b77ec1`). The fork checkout at `~/dev/agent-fork`
has a **stale 2026.7.2 working tree** — every fork citation below is
`git show v2026.8.1-isol8.3:<path>`, never the working tree. Previous shipped
version for diffing: `v2026.7.2-isol8.8`.

---

# THE PLAN

Decided 2026-09-06 against the image we actually run (`v2026.8.1-isol8.3`). Organised by
**what the owner gets**, not by which audit lane found it. Every call below is made; the two
that genuinely need the owner are marked **[OWNER]** and nothing blocks on the rest.

## What 8.1 changes for us, in one paragraph

We upgraded the base and took the token cost without taking the product. 8.1 shipped a live
run-status stream, durable in-flight snapshots, per-session run outcomes, agent-authored
status notes, structured activity items, live usage, and a proxy that can expose an agent's
running dev server to its owner — **all of it is running in our containers right now and we
consume none of it.** Meanwhile three things it changed under us are actively costing us: it
deleted two model refs we pin, it made assistant text block-cumulative (which breaks our
transcript resync), and it hid every non-coding tool behind a search our seed agent doesn't
know to issue. The plan is: stop the bleeding, then spend the release.

## Rollout lanes — this drives everything

| lane | reaches an owner when |
|---|---|
| **BE** backend | dev on merge; **prod only on `workflow_dispatch`** |
| **FE** frontend | dev on merge; **prod only on dispatch** |
| **CFG** `config.py` → `openclaw.json` | **only on reprovision or `PATCH /container/config`** |
| **IMG** Dockerfile / plugins / baked skills | rebuild → ECR → human flips `dev.tag`, validates, then `prod.tag` |

**IMG is slowest — its worktree opens first and lands last.** `main` is ahead of prod by
default; nothing ships to an owner without a dispatch.

## Worktrees — split by file collision, not by feature

| worktree | owns | lane |
|---|---|---|
| `wt-proxy` | `routers/websocket_chat.py` | BE |
| `wt-pool` | `core/gateway/{connection_pool,operator_rpc,frame_sender}.py`, `routers/container_recover.py` | BE |
| `wt-config` | `core/containers/config.py` | CFG |
| `wt-friday` | `core/services/friday_identity.py`, `apps/infra/openclaw/skills/**` | BE + IMG |
| `wt-image` | `apps/infra/openclaw/{Dockerfile,plugins/**}`, `openclaw-version.json` | IMG |
| `wt-rail` | `hooks/useAgentSessions.ts`, `components/chat/Session*.tsx`, `WorkspaceSidebar.tsx` | FE |
| `wt-chat` | `components/chat/{MessageList,AgentChatWindow,StreamingStatusBar,ChatErrorCard,ProvisioningStepper}.tsx` **and `hooks/useAgentChat.ts`** | FE |
| `wt-portal` | new: `routers/portal_view.py`, `components/workspace/panels/PortalDockPanel.tsx` | BE + FE |

Only `wt-image` touches `openclaw-version.json`. House rules: branch never `main`, never
`git stash`, ~2 GB each so don't run all eight, not on iCloud Desktop, symlink `node_modules`.

**Two files are contended and cannot be parallelised:**
- **`connection_pool.py`** — the liveness plan (Tasks 2–4) and the transcript plan (Tasks 3+)
  both edit it, and the transcript plan may convert `_transform_agent_event` from a
  `@staticmethod`. Land transcript first, then rebase liveness. Also note **B1's one-line
  approvals-capability fix lives in this same file** (`:899`), so the cost-and-correctness work
  queues here too.
- **`hooks/useAgentChat.ts`** — liveness Task 5 and transcript Task 4. One worktree, one agent,
  transcript's `chat.history` hydration first.

**`routers/websocket_chat.py` belongs to `wt-proxy` alone.** The rail plan's Task 2 deletes a
dead function from it; fold that into the security branch rather than opening a second branch on
the file.

### ⚠️ Three P0s have no plan yet

Written 2026-09-07 after a plan review found them orphaned. The four plans that exist
(Friday, liveness, transcript, rail) contain **no task** for any of these:

- **C1** — we still pin `claude-sonnet-4-6` and `claude-opus-4-6`, both deleted from 8.1's
  catalog. BYO-Anthropic owners silently degrade to Haiku; Friday's Opus pin has no fallback.
  Two literals in `config.py:642` and `:886`, plus a fleet `PATCH /container/config` to land it.
- **SEC-1 / G-F1 / G-F3 / G-F4** — the proxy tenant boundary. G-F4 alone (reject
  `sessions.patch {permissionMode:"full"}` at the params level) is half a day and closes a live
  cross-member escalation on shared-container orgs.
- **B1** — `caps=["tool-events","approvals"]`. One line, and it is the prerequisite for both
  the liveness plan's approval state and the exec-approvals work.

**Do not let the four written plans ship while these sit unowned.** They are smaller than any of
the four and higher-severity than most of what is in them.

---

# OUTCOME 1 — Friday does the work you asked, instead of shopping for someone to do it

**The decision: fix the prompt. Do NOT touch tool configuration.**

> **CORRECTED 2026-09-07 — an earlier version of this section said "turn Tool Search OFF".
> That was wrong.** `marketplace_search` is a plugin tool and is compacted *identically* to
> `browser` (`isDirectVisibleCatalogTool` requires `sourceName === "core"`,
> `tool-search-catalog.ts:243-252`), so Friday reached one through the compacted catalog in
> the same turn she failed to reach the other. The bounded capability directory is in the
> prompt in every mode (`attempt.ts:342` gates on `toolSearchControlsEnabledForRun`, not on
> mode), listing `browser` by name; and `tool_call` accepts an exact name, so a hidden tool is
> one call away. **Compaction did not stop her finding a tool — the prompt told her to find the
> wrong one.** See H1 below for the full correction, and
> `2026-09-06-friday-uses-her-own-tools.md` for the plan that supersedes this section.

These are one change, not two. Tool Search is on (`config.py:2074` — setting `mode` alone
flips `enabled`), and compaction keeps only `base-coding` and `shell` families visible, so
**`browser` is not in Friday's tool list until she issues a `tool_search`.** Our own comment
argues the token case well, and it was right when written — but an agent that cannot see its
tools is strictly worse than a larger prompt, and we have far cheaper tokens to cut:

- **E1** — our two SKILL.md files are **118 KB**. A turn reading both burns ~30k tokens,
  ~10× the entire skill catalog. Split into thin skill + `references/` read on demand.
- **E3** — `skills.allowBundled` (19 entries) drops ~1,220 tokens/turn of skills that cannot
  run in a headless container.

Cutting those two funds the full tool catalog several times over. **Net token win AND Friday
can see her browser.** There is no per-agent override at this tag, so it is fleet-wide.

| # | change | file | wt | lane |
|---|---|---|---|---|
| H1 | ~~`toolSearch.enabled: False`~~ **DROPPED — the diagnosis was wrong, see above.** Optional successor: `TOOL_SEARCH_MODE` `"tools"` → `"directory"`, which restores upstream's capability-conditioned prompt sections. Separate plan, gated on a dev run. | `config.py` | `wt-config` | CFG |
| E2 | `name: Lightbulb` → `lightbulb` (**blocks E3**) | `skills/lightbulb/SKILL.md` | `wt-friday` | IMG |
| F2 | SOUL: the in-container browser is always there; the paired desktop is the upgrade | `friday_identity.py` | `wt-friday` | BE |
| F1 | AGENTS: step-0 do-it-yourself gate; marketplace check scoped to a missing **role** | `friday_identity.py` | `wt-friday` | BE |
| F4 | byte-exact block migration so existing Fridays get F1/F2 | `friday_identity.py` | `wt-friday` | BE |
| E3 | `skills.allowBundled` — ground-truth with `openclaw skills list --eligible --json` first | `config.py` | `wt-config` | CFG |
| E1 | split both SKILL.md files | `skills/**` | `wt-friday` | IMG |
| F3 | tighten `marketplace_search`'s description to require a delegable role | `plugins/lightbulb-marketplace` | `wt-image` | IMG |

**Done when:** on a dev container, "open flickmeon.ai and tell me what the pricing page says"
drives the browser tool on the first turn, with no `marketplace_search` call.

---

# OUTCOME 2 — You can tell whether your agents are working or stuck

Today we answer the owner's most common question with the word "Working…". 8.1 hands us
everything needed to answer it properly, on the stream we already subscribe to.

| # | change | file | wt | lane |
|---|---|---|---|---|
| D3 | forward `run_status` startup phases (`preparing_context`, `starting_model`) | `connection_pool.py` | `wt-pool` | BE |
| D8 | forward `stream:"usage"` live token counter | `connection_pool.py` | `wt-pool` | BE |
| D9 | forward `stream:"compaction"` notices | `connection_pool.py` | `wt-pool` | BE |
| A2 | `sessions.observer.visibility {visible:true}` — free headline. **Do not add `sessions.messages.subscribe`; it flips the observer to billed.** | `connection_pool.py` | `wt-pool` | BE |
| A8 | carry `status` / `lastRunError` / `abortedLastRun` / `agentStatus` onto the row | `useAgentSessions.ts` | `wt-rail` | FE |
| UI1 | elapsed time + turn recap + parked "Waiting for approval…", `aria-live="off"` | `StreamingStatusBar.tsx` | `wt-chat` | FE |
| UI0 | drop `aria-live` from the ticking provisioning clock | `ProvisioningStepper.tsx` | `wt-chat` | FE |
| UI2 | running ring + approval badge on the roster row, rolled up from sessions | `AgentRoster.tsx` | `wt-chat` | FE |

**Decided:** take the **free** observer headline only. The `health` verdict (`stuck`,
`waiting-on-user`) needs the model tier and draws against the $3/day trial cap — revisit once
the free headline is shipped and we can see whether it's enough.

**Done when:** a run shows a ticking clock and a named phase before the first token, a failed
routine is visually distinct from a successful one, and an owner can see Atlas is blocked
while reading Friday.

---

# OUTCOME 3 — You can see what your agents built

**Portals. Build it.** This is the only item in the release that adds a capability rather
than repairing one, and it closes a real gap: our owners' agents build things and there is no
way to look at them.

The tool is **already live in every container** and currently hands owners a dead
`localhost:<port>` URL, because the listener binds ephemeral and formats against `0.0.0.0`.
The security-group rule (`service-stack.ts:297-304`, all ports backend→container) and our
operator scopes already permit it. `routers/browser_view.py` is the template: resolve the
container IP through Cloud Map per connection, **never cache it**, relay. Portals additionally
need an HTTP reverse proxy, and the WS half (HMR) must ride the `bv-` CloudFront door because
the API Gateway HTTP API cannot pass upgrades.

| # | change | file | wt | lane |
|---|---|---|---|---|
| H2a | **same-day:** deny the `portal` tool until the door exists — stop handing out dead URLs | `config.py` | `wt-config` | CFG |
| H2b | backend portal proxy | `routers/portal_view.py` | `wt-portal` | BE |
| H2c | `PanelKind = "portal"` + iframe panel fed by a `portal.list` poll | `panelRegistry.ts` | `wt-portal` | FE |
| H5 | deny the rest of the unrenderable set (`suggest_task`, `get_goal`, `github_publish`, `terminal`) | `config.py` | `wt-config` | CFG |

**Done when:** an agent runs `npm run dev`, calls `portal`, and the owner clicks a working
link in the workspace.

---

# OUTCOME 4 — The transcript can be trusted

**D1 is the one shipping correctness bug in the whole audit** and leads this group.

| # | change | file | wt | lane |
|---|---|---|---|---|
| D1 | track `itemId` per run; emit `_full_content = prefix + text` so resync stops truncating | `connection_pool.py` | `wt-pool` | BE |
| D4 | read `inFlightRun` off the `chat.history` response we already make — restores in-flight text and pills across a reload | `useAgentChat.ts` | `wt-chat` | FE |
| D7 | act on `stream:"error"` `reason:"seq gap"` — resync deliberately, not only on our own send failure | `connection_pool.py` | `wt-pool` | BE |
| UI3 | jump-to-latest, gated on scrolled-away **AND** content-arrived (never either alone) | `MessageList.tsx` | `wt-chat` | FE |
| UI7 | wire or delete `ChatErrorCard`'s unreachable Retry | `ChatErrorCard.tsx` | `wt-chat` | FE |
| UI14 | "Fix on our end" vs "Fix on yours" on error cards | `ChatErrorCard.tsx` | `wt-chat` | FE |

**Done when:** reloading mid-run restores the partial reply and its tool pills, and a run that
interleaves text → tool → text survives a reconnect intact.

---

# OUTCOME 5 — The session rail is usable

| # | change | file | wt | lane |
|---|---|---|---|---|
| A3 | pass `agentId` to `sessions.list`; delete the 10-page × 100-row walk; use `totalCount` | `useAgentSessions.ts` | `wt-rail` | FE |
| A4 | delete dead `title`/`lastMessage.text`, the no-op preview trim, `includeLastMessage` | `useAgentSessions.ts`, `websocket_chat.py` | `wt-rail` | FE+BE |
| S1 | remove the sidebar search bar; route transcript search into the ⌘K palette we already have | `WorkspaceSidebar.tsx` | `wt-rail` | FE |
| S2 | keep "General" (it carries onboarding kickoff + deployed-agent cron), name it honestly, unpin it, mint a real session for the first typed message | `useAgentSessions.ts` | `wt-rail` | FE |
| S3 | auto-title once via `sessions.patch {label}` after the first exchange | `useAgentSessions.ts` | `wt-rail` | FE |
| UI4 | relative timestamp + one-line preview per row (our Expo app already does this) | `SessionList.tsx` | `wt-rail` | FE |
| A9 | consume the `sessions.changed` row snapshot; demote the 30s poll to reconcile | `connection_pool.py` | `wt-pool` | BE |
| A6 | archive as the default action; delete behind a second confirm | `SessionList.tsx` | `wt-rail` | FE |

**Done when:** a new session appears within a second or two, named something a person would
recognise, and "General" reads as what it is.

---

# OUTCOME 6 — It is safe to put a second person in an org

**Decided: fix the boundary now; defer identity until a trigger fires.**

Our proxy is the *only* thing separating org members, and it forwards any method string.
That is not deferrable — we sell Teams at $50 base + $20/seat, so multi-member orgs are a
product we already take money for.

| # | change | file | wt | lane |
|---|---|---|---|---|
| G-F4 | params-level reject of `sessions.patch {permissionMode:"full"}` — live self-escalation to exec outside the workspace | `websocket_chat.py` | `wt-proxy` | BE |
| G-F1 | vendor `SESSION_TARGET_FIELDS_BY_METHOD` (~52 methods) + the three companion sets; iterate **every** field (`sessions.create` has two). Closes `chat.send`. | `websocket_chat.py` | `wt-proxy` | BE |
| G-F3 | default-deny method allowlist — closes what G-F1 structurally cannot (`secrets.store.*`, `terminal.*`, `users.list`) | `websocket_chat.py` | `wt-proxy` | BE |
| — | regression test that fails when a new key-bearing method appears unguarded; pin + diff the vendored table on each base bump | `tests/` | `wt-proxy` | BE |

**[OWNER] Per-member identity — the one real strategic call.** Everything 8.1 built for
multi-user (roles, attribution, presence, per-requester OAuth) keys off a profile that only
trusted-proxy or Tailscale auth can mint. Today an org has **no attribution at all** — no
`createdActor`, no `sender`. It is 2–4 weeks, mostly re-keying the owner-keyed pool maps, and
the device handshake coexists with trusted-proxy so the control plane keeps its credential.
We tried this once (`ae2ca6086`) and rolled back, but that predates profiles existing.
**My recommendation: schedule it when a second paying multi-seat org lands.** Waiting costs
compounding interest — every week adds code written against owner-keyed assumptions.
**Never drop `operator.admin` alone: every org member goes read-only.**

---

# OUTCOME 7 — It costs less to run and is cheaper to keep

| # | change | why | wt | lane |
|---|---|---|---|---|
| C1 | `claude-sonnet-4-6`/`opus-4-6` → `-5` — **8.1 deleted both**; BYO-Anthropic degrades to Haiku, Friday's Opus pin has no fallback | | `wt-config` | CFG |
| B1 | `caps=["tool-events","approvals"]` — plugin/system-agent approvals currently die with `decision: null` | one line | `wt-pool` | BE |
| A5 | `sessions.describe {key}` replaces a **full-store scan after every chat turn** on a 1-vCPU box | | `wt-pool` | BE |
| D2 | `isHeartbeat` filter — we stream ~48 runs/day/agent to browsers that discard every frame | one line | `wt-pool` | BE |
| H4 | `utilityModel: ""` on the `chatgpt_oauth` path — auto-derivation aims `gpt-5.6-luna` at a Codex token | | `wt-config` | CFG |
| C2 | re-decide `maxDiskBytes: "500mb"` — at 8.1 it drives a new eviction subsystem upstream sized for 10 GiB; we evict at 400 MB **per agent** | | `wt-config` | CFG |
| B2/B3 | restart verb: `config.apply {}` → `gateway.restart.request`; recovery's `update.run` → same. **Not P0** — recovery degrades to reprovision, which works | | `wt-pool` | BE |
| H3 | `OPENCLAW_SUPERVISOR_MODE=external` — blocks an agent running `openclaw update` under a pinned image. **After B2/B3.** CDK: `cdk synth` + commit `deploy.yml` | | `wt-image` | IMG |
| E4/E8 | delete 5 dead binaries (`himalaya`, the `obsidian-cli` alias that never worked, `goplaces`, `wacli`, `1password-cli`); pin the 11 `@latest` installs | | `wt-image` | IMG |
| E5 | `plugins.entries.canvas.enabled: False` — its skill is in the prompt while we deny the tool | | `wt-config` | CFG |
| C5/C7 | emit the other four per-surface `agentId` keys; fix `mcp.runtimeScope`'s version gate; cap `subagents.maxConcurrent`; jitter the 03:00 UTC fleet-wide dream | | `wt-config` | CFG |
| B5 | read `server.bootId` off `hello-ok` — the authoritative "gateway restarted, your cache is stale" signal | | `wt-pool` | BE |
| B6 | `cron.runs` — the server-side answer to the routine-telemetry gap | | `wt-pool` | BE |

**CFG items need a rollout step, not just a merge.** Decide per batch: fleet
`PATCH /container/config`, or let them land at next reprovision. **C1 should be pushed** —
BYO-Anthropic owners are degraded until it reaches them.

---

# Sequence

**Week 1 — stop the bleeding + open the slow lane**
`wt-config` C1, H2a, H5 · `wt-pool` B1, D1, D2 · `wt-proxy` G-F4 · `wt-chat` UI0, UI7
· **open `wt-image` now** (E4, E8, F3, E10) — it lands last, so it starts first.
One prod dispatch at the end of the week. Push C1 to the fleet.

**Week 2 — the boundary + Friday**
`wt-proxy` G-F1 → G-F3 → regression test (serial, same file).
In parallel `wt-friday` F2 → F1 → F4 → the AGENTS.md diet. (The former "H1 experiment" is dropped; the Friday plan's Task 0 replaces it — change ONLY the browser line and confirm causation before spending on the rest.)

**Weeks 3–4 — the product wins**
`wt-pool` D3, D8, D9, A2, A5, A9 · `wt-rail` A3, A4, S1, S2, S3, A8, UI4 ·
`wt-chat` UI1, UI2, UI3, D4, UI14 · `wt-config` E3 (after E2 ships) ·
`wt-friday` E1 · `wt-image` flip `dev.tag`, validate, flip `prod.tag`.

**Weeks 5–6 — Portals** (`wt-portal`), then **exec approvals** (B1 is its prerequisite;
moves every container off an ungated shell).

**Triggered, not scheduled:** per-member identity **[OWNER]**; the observer's model tier;
G-F8 shared sessions if Teams demand is real.

# Not doing — decided, with reasons in the DROP sections

Cloud workers / Crabbox / device session hosting (a second, worse copy of the ECS
infrastructure we already run). Worktrees, `github_publish`, `projects.*` (we use plain EFS
dirs). Browser profile import as shipped (macOS-host only — but our `browser.sessionState`
fork patch is a cheaper path to the same owner need, worth its own design). Upstream's
automations / `/loop` / workboards / goals (duplicates Paperclip — steal `pacing` into
Paperclip instead). `sessions.groups.*` (gateway-global, leaks across org members). Session
dashboards and widgets (Control-UI-only rendering). Per-requester MCP OAuth (worse than not
having it without identity). Operator roles (inert until identity, and half-fixing them makes
every member read-only).

---

## Status legend

- `TODO` — agreed, not started
- `WIP` — in progress (name the branch)
- `DONE` — shipped (name the PR; then delete the row on the next pass)
- `HOLD` — deliberately deferred, reason given
- `DROP` — investigated and rejected, reason given so nobody re-litigates it

## How findings are classified

`LEGACY` we're on a retired pattern · `UNUSED` capability ships in our image and we
never call it · `MISWIRED` we call it but wrongly · `DEAD-WEIGHT` cost with no benefit
· `SECURITY` · `UNREACHABLE` exists but can't work for us.

---

## P0 — security

### SEC-1 · No method allowlist in the RPC proxy (cross-member IDOR in orgs)

**Status:** TODO — fix on its own branch, ahead of and separate from the UX work.
**Class:** SECURITY / MISWIRED. **Effort:** medium.

`routers/websocket_chat.py` forwards whatever `method` string the browser sends to
`pool.send_rpc` (`:2069-2076`). There is no allowlist — verified by grep, nothing
gates `method`. The only tenant fence is an **opt-in** map of method → key-param
(`:1418-1437`) covering 8 methods, plus a list-param map for `sessions.search`
(`:1435-1437`).

Orgs share one container: `owner_id = org_id or user_id`, `user_id = connection["user_id"]`
(`:902-903`). So for an org, that map is the entire boundary between member A and
member B.

8.1 added ~12 more session-key-bearing methods that are **not** in the map and are
reachable today from any org member's browser:

| method | key param | effect |
|---|---|---|
| `sessions.preview` | `keys[]` | reads another member's transcript (`sessions-read.ts:540`, `operator.read`) |
| `sessions.patchMany` | `targets[].key` | routes around the `sessions.patch` guard (`sessions-patch.ts:96-105`) |
| `sessions.messages.subscribe` | `key` | live message stream of another member's session |
| `sessions.companion.ask` | `sessionKey` | model-summarises another member's transcript |
| `sessions.fork` / `compact` / `compaction.restore` / `recover` | various | mutate another member's session |
| `sessions.goal.*` / `assignOwner` / `files.*` / `diff` | various | read/mutate |
| `progressCard.get` / `put`, `session.members.add`, `sessions.visibility.set`, `sessions.viewers.set` | various | read/mutate |

The map's own docstring states the invariant it now violates: *"ANY new gateway method
that takes a caller-supplied session key belongs in this table."*

Why the container doesn't catch it: `roleVisibilityFilter` only engages when an
operator role is configured (`operator-role-policy.ts:138-140`, `hasOperatorBoundary`
→ `operatorSessionCap`). We configure none, and our device holds `operator.admin`
(`core/crypto/operator_device.py:86-89`), so `isGatewayAdmin` short-circuits every
filter.

**UPGRADED 2026-09-06 by the identity lane — it is worse than "read another member's
transcript", and the fix is smaller than hand-writing a map.**

**`chat.send` is unguarded through the generic proxy.** Our dedicated chat path is guarded,
but the passthrough at `websocket_chat.py:2041-2077` forwards a raw
`{type:"req", method:"chat.send", params:{sessionKey:…}}` with no check. That is a member
**writing into another member's conversation**, not merely reading one.

**Upstream maintains our map for us, exhaustively.** VERIFIED at the tag:
`src/gateway/session-sharing-target-input.ts:19-75` exports
`SESSION_TARGET_FIELDS_BY_METHOD` — 41 rows expanding to ~52 methods, each mapping method →
`("key" | "sessionKey" | "parentSessionKey")[]`, with companion sets
`REQUIRED_SESSION_TARGET_METHODS` (`:77-121`), `APPROVAL_SESSION_TARGET_METHODS` (`:123-127`),
`READ_ONLY_SESSION_TARGET_METHODS` (`:129-132`) and the accessor
`sessionMutationTargetFields(method)` (`:144-148`).
Methods it covers that our 8-entry map does not, each a live cross-member write:
`chat.send`, `chat.inject`, `chat.abort`, `sessions.send`, `sessions.steer`, `sessions.abort`,
`sessions.fork`, `sessions.compact`, `sessions.move`, `sessions.dispatch`, `sessions.recover`,
`sessions.goal.update/clear`, `sessions.assignOwner`, `sessions.files.set`,
`sessions.github.publish`, `tools.invoke`, `mcp.app.callTool`, `message.action`,
`plugins.sessionAction`, `session.discussion.open`, `board.*`, `talk.*`, `wake`, and the
`*.approval.resolve` methods. Note `sessions.create` carries **two** fields
(`key` AND `parentSessionKey`) — our map has no concept of multi-field.

**Fix, in this order:**
1. **Port the registry** (G-F1) — vendor the three sets into Python and iterate *every* field
   the table names, not one. **Pin the file and diff it on each base bump** — this is exactly
   the vendored-drift class that already bit us (`feedback_base_bump_must_repin_vendored_schema`).
2. **Add a default-deny method allowlist** (G-F3) — the registry structurally cannot cover
   admin methods that carry no session key at all.
3. **Add a params-level check for `permissionMode`** (G-F4) — an ownership check passes on
   your own key and is the wrong shape for this one.
4. A regression test that fails when a new key-bearing method appears unguarded.

**Bounds (for honest prioritisation):** needs a multi-member org — a personal owner
can only reach their own sessions — and needs hand-crafted WS frames, not a UI click.

---

## P1 — Friday behaves wrongly (the reported bug)

Reported: Friday ran `marketplace_search` for a simple "go to the browser" request.
All of these live in `apps/backend/core/services/friday_identity.py`.

### F1 · Orchestration loop has no do-it-yourself exit
**Status:** TODO. **Class:** MISWIRED. **Effort:** small.
AGENTS.md's loop starts at *Decompose*; step 1's escape hatch is "if it's a single
piece, skip to step 4", and step 4 is *Confirm the delegation plan*. No rung says
"just do it." Step 3 then mandates `marketplace_search` whenever "no agent is
qualified" — and a browser request has no assigned agent, so it qualifies.
**Fix:** step-0 gate — if you can finish it with your own tools this turn, do it;
the loop is for work that needs the team. Name the triggers (browse, fetch, read/write
a file, run a command, answer a question).

### F2 · The browser line is inverted
**Status:** TODO. **Class:** MISWIRED. **Effort:** small. *(Root cause of the report.)*
SOUL.md: *"**Browser** — drive the user's signed-in Chrome via CDP (when their desktop
app is paired)"*. Config says the opposite: `browser.enabled: true`,
`defaultProfile: "openclaw"` (`core/containers/config.py:2315-2322`) — container
Chromium, headful on Xvfb, **always present**; the desktop `user` profile is the
optional upgrade, routable only when a node is paired. Friday's own docs invert which
browser is the default, so "go to the browser" reads as *might not have one*.
**Fix:** state that the in-container browser is always available; the paired desktop
is the upgrade for signed-in sites.

### F3 · `marketplace_search` tool description is too eager
**Status:** TODO. **Class:** MISWIRED. **Effort:** small, but **image lane** —
needs a rebuild + dev→prod tag promotion, so it lands slower than F1/F2/F4.
`apps/infra/openclaw/plugins/lightbulb-marketplace/src/index.ts` — *"Use whenever a
task needs a capability nobody on the roster has."* A missing *tool* reads as a
missing *capability*. **Fix:** require a delegable **role**, and exclude anything the
agent's own tools already cover.

### F4 · Existing Fridays won't get F1/F2
**Status:** TODO. **Class:** —. **Effort:** medium.
Identity files are written `write_file_if_missing`, so prompt fixes reach **new
provisions only**. `friday_identity.py` already has the mechanism: a byte-exact block
replacement (`_LEGACY_NIGHTLY_WAKE_SECTION`) that heals an unedited section and leaves
an owner-edited one alone. Reuse it for the orchestration loop + Boundaries + the
SOUL browser line.

### F5 · `skills.allowBundled` unset — ~52 bundled skills in every prompt
**Status:** TODO — **awaiting the skills lane** for the eligibility list.
**Class:** DEAD-WEIGHT / PROMPT-NOISE. **Effort:** small once the list exists.
Every eligible skill's name + description + location is injected into
`<available_skills>` on **every turn** (`src/agents/system-prompt.ts`,
`buildSkillsSection`). The fork ships ~52 bundled skills; many are macOS-only or
useless headless (`apple-notes`, `bear-notes`, `things-mac`, `sonoscli`,
`spotify-player`, `openhue`, `camsnap`, `meme-maker`, `gifgrep`, `songsee`, `trello`).
`skills.allowBundled` is an allowlist affecting only bundled skills; we never set it
(`core/containers/config.py:2123`).

---

## P1 — the session rail (the other reported issue)

Reported: names look weird, new sessions take forever to appear, the "General" row is
confusing, and the search bar isn't wanted.

### S1 · Remove the session search bar
**Status:** TODO. **Effort:** small.
One usage site: `components/workspace/WorkspaceSidebar.tsx:211`. Deleting it drops
`components/chat/SessionSearch.tsx`, `hooks/useSessionSearch.ts` and two test files.
Upstream independently moved session search into the command palette (#128356).

### S2 · "General" is the platform's channel, not a peer session
**Status:** TODO — design settled, see below. **Effort:** medium.
The 3-part key `agent:{agentId}:{userId}`. **Do not hide or skip it** — we route
platform traffic there:
- onboarding kickoff + first-contact nudge — `core/services/bootstrap_dispatcher.py:1051`, `:1215` (`build_chat_session_key(..., None)`)
- deployed marketplace agents' cron jobs — `core/services/catalog_service.py:353`

A fresh agent defaults to `sessionId = null`, so the owner's first chat lands there too.
In 8.1 it is **not** special: `classifySessionKind` returns `"direct"` for 3- and
4-part keys alike (`src/sessions/classify-session-kind.ts`), and 8.1 retired the
concept (#123521, *"migrate legacy main-session history to its clear owner, and reuse
`main` as an ordinary agent ID"*).
**Fix:** keep the row, name it honestly, unpin it, sort by recency, and mint a real
session for the owner's first typed message.

### S3 · Sessions are named after the first user message
**Status:** TODO — owner chose "agent titles it once". **Effort:** medium.
`derivedTitle` = first user message truncated (`src/gateway/session-utils.ts:165-196`,
`deriveSessionTitle`). **There is no LLM auto-titling upstream** — `/name` with no
argument returns a deterministic suggestion, comment says *"no LLM, no mutation"*
(`src/auto-reply/reply/commands-name.ts`). So this is ours to build: one
`sessions.patch {label}` after the first exchange.

### S4 · New sessions are slow to appear — see A3, A4, A9
Root causes are the 10-page `sessions.list` walk, a per-session transcript read we
throw away, and polling instead of consuming the push we already receive.

---

## P2 — from the session-surface lane

### A2 · ~~`sessions.observer.visibility` — free headlines~~ — **REVISED 2026-09-07: not worth taking**

> **Implementation disproved this finding. Do not build it.**
>
> At our `broad` audience tier the ONLY thing that publishes a digest is the preamble
> publisher, and `session-observer-preamble.ts:71` returns unless
> `event.stream === "item" && event.data.kind === "preamble"`. That is the assistant's own
> commentary text — which `_transform_agent_event` **already forwards** via its
> `("plan","patch","item","acp")` branch, and which the frontend already renders.
>
> So the RPC would open a second delivery route, on every connect, for content we already
> receive, with nothing reading the new field. The audit framed this as "free headlines we're
> missing". We are not missing them; the observer would merely repackage them. The genuinely
> new content — `health`, `assessment`, `planProgress` — needs the `direct` tier, which bills
> the utility model per digest cycle against the $3.00/day trial cap.
>
> The cost tripwire below still stands and is still worth knowing: **never add
> `sessions.messages.subscribe`**, because that is what flips the tier. Keeping this entry
> rather than deleting it so the next reader does not re-derive the same wrong conclusion from
> the same docs.

### ~~A2 (original finding, retained for the reasoning)~~
**Status:** TODO. **Class:** UNUSED. **Effort:** small. *(Wanted.)*
The observer publishes `session.observer` digests `{headline ≤120ch, health,
planProgress, revision}` and persists them onto the row as `observerDigest`
(`session-utils-row.ts:492-501`), delivered on `sessions.changed`. Gated on
`gateway.controlUi.sessionObserver !== false` (`session-observer.ts:251`, `:459`) —
**not** on `controlUi.enabled`, which is the one we set false (`config.py:1621`).
We already `sessions.subscribe` on connect (`connection_pool.py:953-970`), but
`visibleConnections` starts empty and only `sessions.observer.visibility` populates it
(`session-observer-rpc.ts:43`), so the observer classifies every session as unwatched
and suspends.
**Fix:** send `sessions.observer.visibility {visible:true}` next to the existing
subscribe; read `observerDigest.headline` off `sessions.changed`.
**Cost — ADJUDICATED 2026-09-06.** The session lane and the event lane disagreed;
the session lane is right, verified directly. `session-observer.ts:669-672`:
`utilityModelRef = disabledRuns.has(...) || currentAudience !== "direct" ? undefined
: modelSlots.claim(...)`. And `session-observer-audience.ts:40-53` `classify()` returns
`"direct"` ONLY when a **`sessions.messages.subscribe`** subscriber is visible;
a plain `sessions.subscribe` subscriber gets `"broad"`. We only do the latter, so we
land in `broad`, `utilityModelRef` is `undefined`, and no model is claimed — preamble
publisher only, **no extra inference**.

> **TRIPWIRE — do not decouple these.** The moment anything calls
> `sessions.messages.subscribe` on a visible connection, the tier flips to `"direct"`
> and the observer starts billing the utility model per digest cycle. Several other
> findings tempt us toward that RPC. If one lands, re-check this cost, and gate
> `trial_local` owners explicitly ($3.00/day cap). Trial owners safe by construction: `utilityModel` is
omitted on trial (`config.py:1850`) and the Bedrock extension declares no
`defaultUtilityModel`. The richer model-written tier needs per-key
`sessions.messages.subscribe`, caps at 6 concurrent sessions, and bills the utility
model — **not** what we're turning on.

### A3 · `sessions.list` IS agent-filterable — three call sites believe otherwise
**Status:** TODO. **Class:** MISWIRED. **Effort:** small. *(Most of S4.)*
`agentId` is a first-class list param (`packages/gateway-protocol/src/schema/sessions-list.ts:43`);
it narrows the row filter **and the store load** (`sessions-read.ts:262-266`). The
"agentId requires sessionKeys" rule belongs to `sessions.search`
(`sessions-read.ts:114-122`), not `sessions.list`.
Wrong claim in three places: `hooks/useAgentSessions.ts:110-124` (why the sidebar
walks 10 pages × 100 rows of every agent's store and filters client-side, `:135`),
and `core/services/admin_service.py:577-578`, `:606`.
Also unused on the same call: `sortBy`, `requireLastInteraction`, `activeMinutes`,
`label`, `search`, and `totalCount` (`session-utils-list.ts:438`) — which the page
walk exists to approximate.
**Do NOT use** `involvingMe` / `ownerId` / `creatorId` / `ownerFirst`: they resolve
against the single shared backend operator identity (`sessions-read.ts:296-298`), not
the end user, so they cannot replace `_filter_sessions_for_user`.

### A4 · Dead fields, a no-op function, and a transcript read we pay for and discard
**Status:** TODO. **Class:** MISWIRED / DEAD-WEIGHT. **Effort:** small.
The row field is **`lastMessagePreview`**, top-level, capped at 240 chars upstream
(`session-utils-row.ts:475`). There is no `lastMessage` object and no `title` — not in
7.2 either.
- `hooks/useAgentSessions.ts:42-43` declares `title` and `lastMessage?.{text}`; `:74`
  uses both in the label fallback chain. Both branches unreachable.
- `routers/websocket_chat.py:335-350` `_slim_sessions_list_previews` reads
  `row["lastMessage"]["text"]` — **a no-op on every row**, and redundant anyway
  (upstream's 240 < our 280 cap, `:288`).
- `useAgentSessions.ts:126` and `admin_service.py:590` pass `includeLastMessage: true`,
  costing a bounded transcript read **per session row** on a 1-vCPU box, for a field
  nothing reads.

### A5 · `sessions.describe {key}` replaces a full-store scan after EVERY chat turn
**Status:** TODO. **Class:** LEGACY. **Effort:** small. *(Owner explicitly wants this.)*
`connection_pool.py:1674-1700` calls `sessions.list {}` after every turn and linear-scans
for one key — an unbounded store scan per turn, on the container. `sessions.describe {key}`
returns one row with `totalTokens`, `inputTokens`, `outputTokens`, `estimatedCostUsd`,
`model`, `modelProvider` (`sessions-read.ts:613-645`, `operator.read`), and has existed
since ≤2026.7.
**Add it to the SEC-1 guard at the same time** — it takes a caller-supplied key.

### A6 · Archive exists; delete is currently the only option
**Status:** TODO. **Class:** UNUSED. **Effort:** medium (UI).
`sessions.patch {archived:true}` (`sessions-patch.ts:37`) sets `archivedAt`; row exposes
`archived`/`archivedAt`/`archivedBy`; `sessions.list {archived: true | "all"}` lists them
(`"all"` is new in 8.1). We only offer `sessions.delete {key, deleteTranscript:true}` —
irreversible. **Fix:** archive as default, delete behind a second confirm.

### A7 · Organization fields on every row, all dropped
**Status:** TODO. **Class:** UNUSED. **Effort:** medium.
`session-utils-row.ts:466-490` emits `label`, `icon`, `color`, `category`,
`pinned`/`pinnedAt`, `unread`/`lastReadAt`/`markedUnreadAt`, all writable via one
`sessions.patch` (`sessions-patch.ts:19-41`, plus `expectedSessionId` /
`expectedLifecycleRevision` optimistic-concurrency guards we don't send).
The field is **`category`**, not `group`.
**Do NOT use `sessions.groups.*`:** the catalog is gateway-global with no per-client
scoping (`sessions-groups.ts:39-49`), so on our shared container one member's group
names leak to all. Per-session `category` is the safe half.

### A8 · Run outcome is invisible in the session list
**Status:** TODO. **Class:** UNUSED. **Effort:** small–medium.
Row carries `status` (`queued|running|done|failed|killed|timeout`), `lastRunError`,
`lastRunId`, `abortedLastRun`, `hasAutomation` (`session-utils-row.ts:504-537`) and
`agentStatus` — an agent-written, TTL-bounded sidebar note with an attention icon, set
by the `sessions` tool (`src/agents/tools/sessions-tool.ts:127-137`) or
`sessions.patch {statusNote, attention, ttlMinutes}`.
We consume `status` only as a boolean run-active flag (`connection_pool.py:2272-2300`,
`_is_session_run_active` `:3910`). A failed run looks identical to a successful one.
**This is the missing run-outcome signal** from the nightly-CEO telemetry gap.

### A9 · `sessions.changed` already pushes a full row snapshot; we drop all of it
**Status:** TODO. **Class:** UNUSED. **Effort:** medium. *(Pairs with A2 and S4.)*
`broadcastSessionsChanged` embeds `buildGatewaySessionSnapshot` — `updatedAt`, `label`,
`displayName`, `pinned`, `archived`, `unread`, `category`, `color`, `agentStatus`,
`observerDigest`, `owner`, `participants` (`session-change-event.ts:88-118`,
`session-event-payload.ts:15-100`), debounced 100ms / max 500ms. We already receive
these and extract one boolean — while separately polling `sessions.list` on a 30s SWR
dedupe with a 10-page walk.
**Fix:** forward the snapshot as a `session_row` frame, merge in `useAgentSessions`,
demote the poll to reconcile.

### A10 · `sessions.create` removes the "New session" placeholder hack
**Status:** TODO. **Class:** UNUSED. **Effort:** small.
`sessions.create {key, label, idempotencyKey, category, permissionMode, …}`
(`schema/sessions-create.ts:12-25`) materialises a row immediately. We mint a UUID
client-side and wait for the first `chat.send`, which is why `SessionList.tsx:38-44`
carries an `activeIsUnmaterialized` placeholder.

### A11 · `permissionMode` is a real exec fence we never set — and an OPEN escalation today
**Status:** TODO — the **defensive** half (G-F4) is P0 and independent; adopting
`permissionMode` as a *feature* still waits for SEC-1. **Class:** UNUSED + SECURITY.
**Effort:** half a day defensive, medium for adoption.
> See **G-F4**: a member can `sessions.patch {permissionMode:"full"}` on **their own**
> session — so every ownership check passes — and get exec outside the workspace with
> approval floors bypassed. This is live now, not a consequence of adopting the feature.
Per-session `read-only | guarded | workspace | full`
(`schema/sessions-row.ts:12-17`) maps to actual `execMode` + `workspaceOnly` +
`readOnly` core-tool policy (`src/agents/session-permission-exec-mode.ts:10-36`).
This is *enforcement*, unlike `tools.deny`, which is a prompt-surface control that
`exec` + the `openclaw` CLI walk straight past. We set `permissionMode` only on the
unrelated ACP block (`config.py:1414` = `"approve-all"`).

---

## P1 — from the event/stream lane

Our translation layer is `core/gateway/connection_pool.py` `_transform_agent_event`
(1105-1187) plus the chat-state handling in `_handle_message`. Baseline established by
the lane: we connect with `caps=["tool-events"]` and `operator.{admin,read,write}`, and
we `sessions.subscribe` on connect, so we are eligible for **every** `READ_SCOPE` event
and are NOT subject to the `requiresSessionSubscription` gate
(`server-broadcast.ts:110-114`). Protocol version is unchanged at 4 across both tags —
none of this is a handshake problem. Unhandled events fall through
`connection_pool.py:2310`, broadcast raw, and reach zero subscribers.

### D1 · Assistant `text` became block-cumulative in 8.1 — our full-text resync truncates
**Status:** TODO — **highest-value non-security item; a live correctness bug today.**
**Class:** LEGACY-SHAPE. **Effort:** medium.

VERIFIED DIRECTLY. `scopeAssistantMessageToStreamBlock`
(`src/agents/embedded-agent-subscribe.handlers.messages.stream.ts:149-181`, **absent in
7.2**) returns `{...message, content: [block]}` — the current text block only. Its
comment: *"Provider partials are cumulative across content blocks. Once a content index
becomes a logical reply boundary, downstream snapshots must be cumulative only within
that block or earlier text is replayed."* The runtime compensates by carrying the prefix
itself: `server-chat.ts:997-1007` sets `run.assistantScope = {itemId, prefix}` and
`live-chat-projector.ts:66-96` returns `scope.prefix + nextText`. `itemId` on assistant
event data is new in 8.1.

We don't carry the prefix: `connection_pool.py:1213-1222` sets
`_full_content: text or delta` — the raw block-scoped `text`. `frame_sender.py:187-200`
ships that as the authoritative full-text frame on the first frame of a
`(runId, type)` per connection and after any send failure.

**Symptom:** any run that interleaves text → tool_use → text (the normal shape of an
agentic run, not an edge case) will, on a mid-run reconnect, a newly-opened second tab,
or one dropped Management-API POST, **snap back to only the current text block** —
everything the agent said before the last tool call disappears from the bubble. The
delta-append path is fine; only the resync path lies.
**Fix:** track `data["itemId"]` per `runId`; on change, stash accumulated text as prefix
and emit `_full_content = prefix + text`. Needs per-run state in `GatewayConnection` —
`_build_text_frame` is currently a `@staticmethod`, which is why this is M not S.

### D2 · `isHeartbeat` filter missing — cost and frame noise
**Status:** TODO. **Class:** DROPPED. **Effort:** small. *(Best value-per-line here.)*
`server-chat.ts:1410-1421` stamps `isHeartbeat` on every broadcast `agent` payload;
upstream's own chat surface suppresses this output (`server-chat.ts:161-179`). We have
no handler — `_handle_message` gates only on `session_source` (`:1898-1912`).
We set `heartbeat.isolatedSession: True` (`config.py:1835`), producing a
`…:heartbeat`-suffixed key. For an **org** owner that is
`agent:<id>:<clerk_user_id>:heartbeat` — exactly 4 parts, which `_parse_session_key`
(`:4016-4021`) classifies as `source="webchat"` with a real `member_id`.
**Consequence:** every heartbeat run (~48/day/agent) streams its full assistant/thinking/
tool frame set through `_forward_to_frontends` → `FrameSender` → an HTTPS `@connections`
POST **per frame, per open tab**, to be discarded in the browser
(`useAgentChat.ts:1484-1493` demuxes on `session_id`). Personal owners are safe by
accident — `member_id` parses as the literal `"main"`, matching no connection.
**Fix:** `if payload.get("isHeartbeat"): return` beside the channel/cron/paperclip gate.

### D3 · `chat state:"status"` / `stream:"run_status"` — startup phases, dropped twice
**Status:** TODO. **Class:** DROPPED. **Effort:** small.
New in 8.1: `ChatStatusEventSchema` (`schema/logs-chat.ts:245-262`) with a closed enum —
`preparing_workspace, naming_worktree, creating_worktree, running_setup,
provisioning_environment, preparing_context, starting_model` — mirrored on the agent
stream as `stream:"run_status"` (`src/infra/agent-run-status-events.ts:11-17`).
We return early on any non-terminal chat state (`connection_pool.py:1981`), and
`run_status` matches no branch. **The send→first-token gap is dead air with a generic
spinner, while the container is telling us exactly what it's doing.**
**Fix:** map to the existing `agent_activity` frame the frontend already renders.

### D4 · `chat.history` returns `inFlightRun` + `pendingInputs`; we never read them
**Status:** TODO. **Class:** DROPPED. **Effort:** medium (frontend-heavy).
`server-methods/chat-history-handler.ts:531-598` returns `inFlightRun`
(`chat-abort.ts:386-450`), backed by `server-chat-progress-snapshot.ts` (**new in 8.1**),
which durably retains up to 50 tool/item/`run_status`/notice events per run. Its comment:
*"Surface any run still streaming … so a client that switched away … can restore the
in-flight assistant text on switch-back."* `pendingInputs` (`logs-chat.ts:39-55`, new)
returns queued-but-unsent turns with `state ∈ {queued, cancelled, interrupted}`.
`grep inFlightRun|pendingInputs|deltaCursor` across backend, frontend, mobile and shared
→ **zero hits**, against 61 references to `"chat.history"`.
**This is "durable progress cards across reloads", and it needs no new RPC** — it is
already in the response we make today. Reload mid-run now and the pills and partial text
are gone until the run terminates.

### D5 · `tool phase:"input_delta"` — live edit diffs, discarded
**Status:** TODO. **Class:** DROPPED. **Effort:** small backend, small–medium frontend.
`handlers.messages.update.ts:75-84` emits `{phase:"input_delta", toolCallId, name,
diff:{added,removed}}`; producer `embedded-agent-live-edit-diff.ts` is **new in 8.1**.
It carries a `name`, so it enters our `elif name:` branch (`connection_pool.py:1139`) and
falls out as `None`. "Editing foo.py" sits static while a 400-line rewrite streams;
8.1 offers running +added/−removed every 250ms.

### D6 · `spawnedBy` — subagent attribution, on every event, consumed nowhere
**Status:** TODO. **Class:** DROPPED. **Effort:** small backend, medium frontend.
`schema/agent.ts:63`, `logs-chat.ts:233`; populated at `server-chat.ts:1409`, `:1147`.
`grep spawnedBy` across backend, frontend and shared → **zero hits**; we copy only
`runId` (`:1195`). Every chunk and tool pill from a spawned subagent lands in the primary
bubble unattributed.

### D7 · `stream:"error"` `reason:"seq gap"` — the runtime says it dropped frames
**Status:** TODO. **Class:** DROPPED. **Effort:** small–medium.
`server-chat.ts:1447-1469` broadcasts this when `evt.seq !== last + 1`. We have no
`error` branch. This is precisely the trigger our `_full_content` resync exists for, and
the only signal that would let us resync deliberately rather than only on our own send
failure. Continuity is keyed by connection, not run (`frame_sender.py:102-108`), hence M.

### D8 · `stream:"usage"` — live in-run token counter, new in 8.1
**Status:** TODO. **Class:** DROPPED. **Effort:** small backend, medium frontend.
`embedded-agent-subscribe.ts:198-220` emits `{stream:"usage", data:{outputTokens}}` per
completed model call, cumulative per generation (`src/infra/agent-run-usage.ts:16-34`).
Does not exist at 7.2. Matters disproportionately for us: a free-tier owner is on a
**$3.00/day** cap and one first run burns ~42% of it.
**Caveat:** `outputTokens` only — **not** a replacement for the usage RPC (A5), which
also gets input + cache tokens. Keep the RPC for billing; use the stream for the meter.

### D9 · `stream:"compaction"` — context-compaction notices
**Status:** TODO. **Class:** DROPPED. **Effort:** small.
`handlers.compaction.ts:37-56` (`phase:"start"` / `"end"` with `completed`, `willRetry`,
`outcome`, `reason`). Present in 7.2 too — a standing gap, not an 8.1 regression. A long
session stalls for tens of seconds with no explanation; a *failed* compaction shows as an
unexplained pause and possibly a lost-context reply.

### D10 · `item` / `command_output` / `patch` structure flattened
**Status:** TODO. **Class:** HAND-ROLLED. **Effort:** small backend, medium frontend.
`src/infra/agent-activity-events.ts` (**new file in 8.1**) types `AgentItemEventData`
with `itemId`, `phase`, `kind ∈ {tool,command,patch,search,analysis}`, `title`,
`status ∈ {running,completed,failed,blocked}`, `progressText`, `meta`, `error`,
`summary`, `approvalId`; and types `command_output` (`exitCode`, `durationMs`, `cwd`,
`status`) and `patch` (`added[]/modified[]/deleted[]/summary`).
We fold `plan|patch|item|acp` into one opaque `agent_activity`
(`connection_pool.py:1171-1186`) and forward only `output` + `toolCallId` for
`command_output` (`:1161-1169`). No exit code or duration on a finished command; no
failed/blocked distinction; a patch renders as a title instead of a file list.

### D11 · Corrections to comments in our own code
**Status:** TODO (trivial). Two of our comments are now wrong:
- `connection_pool.py:1072-1073` and `frame_sender.py:106` say the coalescing interval
  is **150ms**. At 8.1 it is **75ms** (`server-chat.ts:225` `LIVE_TEXT_PACING_MS`).
  Behaviourally harmless — the transform doesn't read it — but it **doubles our frame
  rate and therefore our Management-API POST volume**. Know this before reading a cost graph.
- `connection_pool.py:1115` handles `stream in ("reasoning","thinking")`. `reasoning` is
  not emitted anywhere at 8.1. Dead branch, harmless.

### Event-lane DROP list
- **`ui.command`** — recipients filtered to `client.id === CONTROL_UI` **and** the
  `ui-commands` cap (`server-methods/ui-command.ts:56-66`). We are `gateway-client`.
  Unreachable without changing our client id, which changes our admission class.
- **`board.command`** — same `CONTROL_UI`-only filter (`dashboard-tool.ts:253-256`).
  Note the sibling `board.changed` IS a plain `READ_SCOPE` broadcast and does reach us,
  so pinned dashboards are half-reachable: we can see a board changed, we cannot receive
  the agent's live widget commands.
- **`terminal.data` / `terminal.exit`** — `ADMIN_SCOPE`, targeted to the owning
  connection; "shared conversation terminals" needs a paired node host.
- **`session.typing`** — `server-broadcast.ts:110-111` makes session subscription
  mandatory regardless of caps; reachable only via `sessions.messages.subscribe`
  (see the A2 tripwire).
- **Correctly ignored:** `health`/`tick` keep-alives; `heartbeat`, `shutdown`,
  `update.available`, `config.changed`, `skills.changed`, `voicewake.*`, `node.*`,
  `device.pair.*`, `portal.changed`, `plugin.*` (control-plane); `chat state:"delta"`
  (the agent stream carries the same text via a separate fan-out); `approval` and
  `lifecycle` (consumed upstream in `_handle_message`); `stdout`/`stderr` (CLI-runner
  surface). **`session.tool` needs a live frame dump to settle** — we advertise
  `tool-events` so we are probably in `runToolRecipients` and therefore excluded from
  it by `excludeConnIds` (`server-chat.ts:1600-1615`), but that is inference, not proof.

---

## P0 — shipped regression from the 8.1 base bump

### C1 · We pin two Anthropic models that 8.1 deleted from the catalog
**Status:** TODO — **live in prod now.** **Class:** LEGACY. **Effort:** small.

VERIFIED DIRECTLY by diffing the two tags' `extensions/anthropic/openclaw.plugin.json`:

| model | 7.2 catalog | 8.1 catalog |
|---|---|---|
| `claude-sonnet-4-6` | present | **gone** |
| `claude-opus-4-6` | present | **gone** |
| `claude-haiku-4-5` | present | present |

8.1 ships `claude-opus-5`, `claude-sonnet-5`, `claude-fable-5`, `claude-mythos-5`,
`claude-opus-4-8` (deprecated), `claude-haiku-4-5`.

We emit both of the deleted ones: `config.py:642` `"primary": "anthropic/claude-sonnet-4-6"`
and `config.py:886` Friday's CEO pin `"anthropic/claude-opus-4-6"`.

**Scope:** the `anthropic/` provider prefix — i.e. **BYO-key Anthropic owners only**.
Bedrock owners address `us.anthropic.*` inference profiles and are unaffected.
**Consequence:** the static catalog no longer resolves these, so they depend entirely on
the plugin's `discovery: {anthropic: "refreshable"}` live refresh against the owner's key.
Before that lands — cold container, first run, key or network failure — `model-setup.ts:130`
throws `FailoverError model_not_found`. The primary silently degrades to the
`claude-haiku-4-5` fallback; **Friday's opus pin has no per-agent fallback at all.**
**Fix:** `claude-sonnet-5` primary, `claude-opus-5` for the CEO. Same correction already
applied on the OpenAI side (`config.py:600-620`, `gpt-5.5 → gpt-5.6-sol`) and never
carried across to Anthropic.

---

## P1 — from the config lane

### C2 · `maxDiskBytes: "500mb"` now drives a subsystem that didn't exist at 7.2
**Status:** TODO — needs a decision, not just an edit. **Class:** LEGACY. **Effort:** small + a call.
`config.py:2302-2306` emits `mode:"enforce"` + `maxDiskBytes:"500mb"`.
`src/config/sessions/session-history-eviction.ts` is **new at 8.1**; its gate is
`mode === "enforce" && maxDiskBytes != null && highWaterBytes != null` (`:476`), and
`store-maintenance.ts:30` sizes the upstream budget at **10 GiB** (high-water 0.8×, `:162`).
At 7.2 this key bounded *archives*. At 8.1 it bounds **main file + WAL + artifacts**, and
crossing it prunes retained archives (`fs.promises.rm`, `:395`) then evicts historical
SQLite sessions oldest-first.
**Consequence:** eviction at **400 MB** instead of 8 GiB — 20× tighter than upstream sized
the new subsystem for. Older conversations vanish from `chat.history` replay. Per-agent
after 8.1's schema restructure, so 400 MB per agent, not per container.

### C3 · Four browser knobs we tuned for Fargate silently reverted
**Status:** TODO — accept-and-document, or fork patch. **Class:** LEGACY. **Effort:** zero or medium.
We correctly drop `tabCleanup.idleMinutes:30`, `maxTabsPerSession:4`,
`localLaunchTimeoutMs:20_000`, `localCdpReadyTimeoutMs:30_000` on 8.1 (`config.py:2337-2342`)
— they're on the dead-key list. But 8.1 **hardcodes** the replacements
(`extensions/browser/src/browser/config.ts:229-238`, only `enabled` is config-readable):
`IDLE_MINUTES=120`, `MAX_TABS_PER_SESSION=8`, `LAUNCH_TIMEOUT_MS=15_000`,
`CDP_READY_TIMEOUT_MS=8_000`.
**Consequence:** up to 8 live Chromium tabs idling 2h on a 1 vCPU / 4 GB task, and the CDP
launch budget back to 15s/8s — the exact "Failed to start Chrome CDP on port 18800" case
`config.py:2400` was raised to avoid. Relevant to F2: the browser Friday is supposed to use.
No config path exists. Our comment notes the schema change but not that the values reverted.

### C4 · `workshop.autonomous.mode:"propose"` does not put a human in the loop
**Status:** TODO — product decision. **Class:** MISWIRED (comment vs behavior). **Effort:** one line + a call.
`config.py:2146-2152` emits `"propose"` for paid, commented "reviewed in the Skill Proposals
panel". But `src/skills/workshop/config.ts:22` defaults `approvalPolicy` to **`"auto"`**, and
`policy.ts:203` returns `undefined` (no prompt) for agent-issued
`skill_workshop apply|reject|quarantine` under `"auto"` — the agent proposes *and* applies
unattended. Not a 7.2→8.1 change; the same at both. But the stated intent is false.
**Careful:** `"pending"` re-introduces the approval-hang failure `_build_exec_policy`
documents for unattended runs (`config.py:525-548`). Not a safe flip.

### C5 · Only one of five per-surface ownership keys is emitted
**Status:** TODO. **Class:** MISWIRED. **Effort:** low.
`src/config/legacy.default-agent-roles.ts:127-140` materializes **five** things from the
legacy `default:true` marker: `heartbeat.agentId`, `systemAgent.agentId`,
`authInheritance.agentId`, `sessionStore.agentId`, `talk.agentId`. We emit only
`systemAgent.agentId` (`config.py:1698`, from #2308). Whatever made that one need an
explicit emit applies to the other four. *(Honest caveat from the lane: `io.load.ts:66`
runs the materializer on every load, so it may self-heal — but if that were reliable,
#2308 would not have been needed.)*

### C6 · `BEDROCK_CONTEXT_TOKENS = 200_000` lands exactly on 8.1's XL tool-result tier
**Status:** TODO — measure first. **Class:** LEGACY. **Effort:** one constant.
`src/agents/tool-result-limits.ts` is **new at 8.1**: `>= 200_000` → **64,000 chars** per
live tool result; `>= 100_000` → 32,000; else 16,000. The old bound
(`contextLimits.toolResultMaxChars`) is retired. So each browser/MCP tool result can inject
~4× the default on the Bedrock path. `tokenjuice` covers exec/bash only.
**Fix:** accept, or `199_000` to land on the 32k tier. Measure before changing.

### C7 · Config-lane smaller items
- **Trial emits a self-fallback** — `model.fallbacks == [primary]` on `bedrock_trial`;
  upstream dedupes failover candidates by provider/model key, so this is zero candidates.
  Dead config (deliberate per `config.py:753`, but buys nothing).
- **`mcp.runtimeScope` version gate reads the wrong source** — `config.py:2604` uses
  `deployed_upstream_at_least("2026.6.1")` while every other gate takes `deployed_tag`.
  Inert today; it is the one gate that could write a newer-only key onto a lagging running
  image, the exact failure `_target_upstream` exists to prevent. One line.
- **`subagents.maxConcurrent` left at upstream 8** — we pin `agents.defaults.maxConcurrent = 4`
  because 1 vCPU can't take more (`config.py:1896-1917`), then leave the subagent lane, which
  explicitly does NOT count against that semaphore, at `DEFAULT_SUBAGENT_MAX_CONCURRENT = 8`.
- **Fleet-wide dreaming at `0 3 * * *` UTC** (`config.py:1230`) — every paid container runs
  the sweep in the same minute. Thundering herd / spend, not correctness. Jitter by owner hash.
- **`modelSelectionScope` unset** → `"effective"` (`sticky-model-selection.ts:22`), so a
  `/model` directive **persists into `agents.defaults` on disk** — chat writing into the file
  the backend owns. `"session"` keeps picker changes ephemeral.
- **`hooks.maxBodyBytes`** correctly dropped; 8.1's built-in cap is 256 KB and the
  replacement is per-mapping. We define no mappings, so the 500 KB GitHub case our comment
  cites now has no route.

### C8 · Vendored schema IS in sync — but the vendor script's default path is wrong
**Status:** TODO (the script). **Effort:** XS.
`PINNED_TAG` matches `openclaw-version.json#upstream`, and regenerating the schema produced a
**byte-identical** file. Good. But `apps/backend/scripts/vendor_openclaw_schema.sh:22` defaults
`FORK` to `$HOME/Desktop/agent` — the stale clone CLAUDE.md says never to use — and writes
`PINNED_TAG` from `openclaw-version.json` regardless of what was actually built, so the
staleness guard can pass on a bundle vendored from the wrong tree.

---

## P1 — from the skills/plugins/image lane

### E1 · Our own two SKILL.md files are 118 KB — bigger than everything else combined
**Status:** TODO. **Class:** PROMPT-NOISE. **Effort:** medium.
VERIFIED: `skills/lightbulb/SKILL.md` = **58,781 bytes**, `skills/paperclip/SKILL.md` =
**60,142 bytes**. ~14.7k / ~15.0k tokens per read. A turn that reads both burns **~30k
tokens — roughly 10× the entire 43-skill catalog** — on a $3/day trial cap where the first
run already burns 42%. Nothing truncates it (`DEFAULT_MAX_SKILL_FILE_BYTES = 256_000`).
And `lightbulb`'s description orders the agent to read it on almost any turn: *"Read BEFORE
answering, not after being corrected — don't skip because a question 'isn't about Lightbulb
mechanics'"*, with a trigger list broad enough to fire on strategy, architecture,
automations, email, or any capability question.
**Fix:** split both into a thin SKILL.md (routing table + non-negotiables) plus
`references/*.md` read on demand — the pattern upstream already uses
(`skills/himalaya/references/`, `skills/1password/references/`) and 8.1's prompt text
explicitly supports (`src/skills/loading/skill-contract.ts:75`).
**This is a bigger prompt win than F5, and it also narrows F1** — a description that fires
on everything is part of why Friday reaches for the wrong thing.

### E2 · `name: Lightbulb` is capitalized — it would silently break F5's allowlist
**Status:** TODO — **do this BEFORE F5.** **Class:** CONTRACT-DRIFT. **Effort:** XS (one character).
VERIFIED: `skills/lightbulb/SKILL.md:2` is `name: Lightbulb`. The bundled loader takes
`frontmatter.name` verbatim with no validation (`src/skills/loading/local-loader.ts:98`);
the `^[a-z0-9-]+$` check exists only on the *session* loader path and only warns.
`isBundledSkillAllowed` matches `allowlist.has(key) || allowlist.has(entry.skill.name)`
(`src/skills/loading/config.ts:113-121`), and `normalizeStringEntries` **does not lowercase**.
So `allowBundled: ["lightbulb"]` would exclude our own most important skill.
Also: its description is 1,150 chars vs the spec's `MAX_DESCRIPTION_LENGTH = 1024` —
unenforced on this path, but over.

### E3 · F5 sized: 43 skills ≈ 3,100 tokens/turn; the allowlist saves ~39%
**Status:** TODO — **this is the concrete list F5 was waiting on.** **Effort:** small.
Measured by rendering the actual block: **38 eligible bundled + 5 plugin-provided =
12,405 chars ≈ 3,100 tokens, every turn, every agent, every owner.**
Note `toolSearch: {mode:"tools"}` (`config.py:2096`) does **not** compact this — `read` is
family `base-coding` and stays directly visible, so `canAccessSkills` is true and the
catalog ships in full.
Of the fork's **51** bundled skills, **36 are eligible** here; 15 are not (4 darwin-only,
6 missing bins, 4 missing env, 1 config-gated). **13 are eligible only because our
Dockerfile deliberately bakes the binary.**
Proposed `allowBundled` (19): `paperclip`, `lightbulb` (after E2), `skill-creator`,
`github`, `gh-issues`, `mcporter`, `summarize`, `session-logs`, `taskflow`, `spike`,
`diagram-maker`, `video-frames`, `nano-pdf`, `python-debugpy`, `node-inspect-debugger`,
`xurl`, `gog`, `tmux`, `clawhub` (borderline — our own skill says "You cannot install
skills for them", `skills/lightbulb/SKILL.md:539`).
Excluded and why: `1password`/`notion`/`oracle`/`gemini`/`goplaces`/`trello` (no
credentials ever provisioned); `eightctl`/`blucli`/`sonoscli`/`openhue`/`spotify-player`
(need the owner's **home LAN** — a Fargate task in a private subnet physically cannot
reach it); `ordercli`/`songsee`/`gifgrep`/`meme-maker`/`blogwatcher` (toys);
`healthcheck` ("audit/harden OpenClaw hosts: SSH, firewall, disk encryption" — wrong
machine, and the worst kind of bait); `node-connect` (`nodes` denied by default);
`weather` (`web_fetch` covers it); `taskflow-inbox-triage` (an example file).
**Plugin skills are NOT covered by `allowBundled`** (`isBundledSkill` matches only sources
`openclaw-bundled`/`openclaw-custodian`, `config.ts:99-104`). Five load: keep
`browser-automation`, `acp-router`, `wiki-maintainer`; drop `canvas` and
`obsidian-vault-maintainer` via `skills.entries.<name>.enabled: false`.

### E4 · Dead image weight — 5 binaries, and one alias that never worked
**Status:** TODO. **Class:** DEAD-WEIGHT. **Effort:** XS each.
- **`himalaya`** (`Dockerfile:218`) — installs to `/usr/local/bin/himalaya`, on PATH, so
  the skill is eligible and in the prompt — while our own skill spends prose countering it:
  *"Lightbulb does not use Gmail, himalaya, or an OpenClaw email channel"*
  (`skills/lightbulb/SKILL.md:14`). Also the only network-fetched binary in the file with
  no sha, no pin, no smoke test — it tracks `master`.
- **`obsidian-cli` symlink** (`Dockerfile:213-215`) — the comment says the skill requires
  bin `obsidian-cli`, but at **both** tags it requires `obsidian`. The alias has never made
  it eligible. Delete the symlink and the `notesmd-cli` clone+build (`:72-73`).
- **`goplaces`** (`:68`) — its skill needs `GOOGLE_PLACES_API_KEY`, never set anywhere.
- **`wacli`** (`:63`) — its skill lives in the `whatsapp` extension, which is in the base
  image's exclusion list AND `EXCLUDED_CORE_BUNDLED_PLUGIN_DIRS`. We don't bake whatsapp.
- **`1password-cli`** (`:148-155`) — no 1Password anywhere in the platform. ~40 MB.
Estimated ~100 MB combined — **unmeasured**; verify with `du -sh` in a running container.

### E5 · `canvas` skill is in the prompt while we deny the `canvas` tool
**Status:** TODO. **Class:** PROMPT-NOISE. **Effort:** XS.
`extensions/canvas/openclaw.plugin.json` is `enabledByDefault: true`, ships (not excluded),
and its skill has no `requires` so it always loads. We deny the tool
(`config.py:287` `TOOL_DENY_BASE`). The agent is told it can *"Present hosted widget
documents on a connected macOS panel"* — a panel we don't have, via a tool we deny.
**Fix:** `plugins.entries.canvas.enabled: False` kills plugin, skill and startup work in one line.

### E6 · `coding-agent` is config-gated off while `claude` + `opencode` are baked
**Status:** TODO — decide. **Class:** MISSING or DEAD-WEIGHT, pick one. **Effort:** small.
The skill's `requires` is `{anyBins:["claude","codex","opencode"], config:["skills.entries.coding-agent.enabled"]}`.
The bins are satisfied (`Dockerfile:269-270`, `:282`); only the config gate blocks it.
Either enable it for **paid** owners — turning "the owner does the coding" into "the agent
delegates to a background Claude Code worker" on binaries already in the image — or record
that the bins stay for ACP, not this skill. **If enabling: gate exactly like
`workshop.autonomous` already is.** Those workers make their own model calls; on a $3/day
trial cap that is ruinous.

### E7 · Swarm is off, so `agents_wait` never surfaces
**Status:** HOLD — paid-only, needs concurrency validation. **Class:** MISSING. **Effort:** small + medium validation.
`resolveSwarmConfig` defaults `enabled: false` (`swarm-config.ts:16`) and
`tool-catalog.ts:542` drops `agents_wait` unless swarm is on. We set
`subagents.maxConcurrent` but never `tools.swarm`. It would let an agent fan work to
siblings and *wait*, instead of handing the owner a to-do list — the exact shape of Friday
orchestrating Atlas and hired agents. Multiplies concurrent model calls; see C7's
`maxConcurrent` note.

### E8 · Image is not reproducible, and comments claim pins that don't exist
**Status:** TODO. **Class:** STALE-PIN. **Effort:** small.
`Dockerfile:44-58` discusses `wacli@v0.8.1` and `gogcli@v0.35.0` as if pinned; **all 11
`go install` lines (:59-69) use `@latest`**. `notesmd-cli` (:72) and `openhue-cli` (:76)
are `git clone --depth=1` of default-branch HEAD; `himalaya` (:218) tracks `master`; `uv`
(:178) is the unpinned installer; five npm globals (:244-246) unpinned. Two rebuilds of the
same commit can produce different binaries. `litestream` (:828-836) already shows the right
pattern — version ARG + SHA256 + smoke test.

### E9 · Externalized plugin bakes are still correct — verified, no action
All six (`slack`, `discord`, `amazon-bedrock`, `lobster`, `parallel`, `firecrawl`) remain
excluded from the shipped image at **both** tags via root `package.json`'s
`"!dist/extensions/<id>/**"` list (94 entries at 8.1 vs 76 at 7.2), and the base builds with
`OPENCLAW_EXTENSIONS=""`. So our bakes are load-bearing, not redundant. No renames; the
`compat.pluginApi` floor moved to `>=2026.8.1`, which our `@2026.8.1` pins match.
Our three first-party plugins are contract-clean: `plugin-entry` is still an export (only
the monolithic `./plugin-sdk` barrel was deleted, which we never imported), all use
`register(api)`, and `openclaw.runtimeExtensions` is still honored.
*Soft gap:* none declares `openclaw.compat.pluginApi` / `openclaw.build.openclawVersion`.
Not enforced today (absent `pluginApi` short-circuits to `{ok:true}`), but adding both gives
a real version fence on the next base bump. XS.

### E10 · Extended-image README documents the wrong base
`apps/infra/openclaw/README.md:3,7,13` still says the base is `ghcr.io/openclaw/openclaw:<UPSTREAM>`.
The Dockerfile is `FROM ghcr.io/thelightbulbcompany/agent:2026.8.1-isol8.3` (:84). The
README's bump steps 2–3 are wrong. XS.

### E11 · Ground-truth the eligibility list before acting on E3
The list is derived, not observed. One command inside a live container settles it
(`src/cli/skills-cli.ts:1227,1277`):
```
openclaw skills list --eligible --json
openclaw skills check --json
```
Trust that over the derived list. Most likely to differ: `gemini` and `summarize`, whose
npm bins are installed `--ignore-scripts` and never smoke-tested.

---

## P0 — two live bugs with one-line fixes (gateway RPC lane)

### B1 · Backend approvals have no delivery route — they expire instantly with `decision: null`
**Status:** TODO. **Class:** MISWIRED. **Effort:** small (one line).

VERIFIED DIRECTLY. `canDeliverApprovals` (`src/gateway/server-request-context.ts:138-164`)
requires the scope **and** one of: `internal.approvalRuntime`, a client id in
`ALL_APPROVAL_CLIENT_IDS` (`{openclaw-control-ui}` only), the `approvals` cap, or a
kind-specific id/cap (`exec` → macos/ios/android or `exec-approvals`; `plugin` → tui or
`plugin-approvals`).

We connect as `gateway-client` (`core/crypto/operator_device.py:95`) with
`caps=["tool-events"]` (`core/gateway/connection_pool.py:899`). **We match none of them.**
`operator.admin` satisfies the scope half but not the "renders it" half — the comment in
the fork says so explicitly: *"Scope grants approval access; it does not prove the client
renders this approval kind."*

**Consequence is worse than a missing event.** `approval-shared.ts:404-434`: with no
approval client and no turn-source route, the record is `manager.expire(id,
"no-approval-route")` and the agent receives `decision: null` **immediately**.
`approval-shared.ts` backs `exec-approval.ts`, `plugin-approval.ts` and `system-agent.ts`.

Scoped honestly:
- **exec** — moot by design; we set `tools.exec.mode: "full"` (`config.py:552-554`), so no
  exec approval is ever created.
- **plugin + system-agent (`openclaw.approval.*`)** — live and silently dead. Our frontend
  subscribes to exactly these (`hooks/useExecApprovals.tsx:462`, `:491`).
- **`question.*` unaffected** — `server-methods/question.ts:337-343` uses plain
  `context.broadcast`, not this gate. `ask_user` / `useQuestions` work.

**Fix:** `caps=["tool-events", "approvals"]` at `connection_pool.py:899`.
**Keep `tool-events`** — it is load-bearing (`agent-turn/principal.ts:31-37`,
`chat-send-agent-dispatch.ts:352-356`).

### B2 · `POST /gateway/restart` has never worked
**Status:** TODO. **Class:** MISWIRED. **Effort:** small.
VERIFIED at **both** tags: `ConfigApplyLikeParamProperties` declares `raw: NonEmptyString`
**non-optional** inside a `closedObject` (`packages/gateway-protocol/src/schema/config.ts:36-48`
— byte-identical at `v2026.7.2-isol8.8`). The handler bails on line one
(`server-methods/config.ts:1204-1207`).
We call it with `params={}` (`routers/container_recover.py:272-283`). Every call returns
`INVALID_REQUEST`; the route then 502s as "Gateway restart failed". **Not an 8.1
regression — it has never worked.**
**Fix:** `gateway.restart.request` with `{"reason": "operator recovery"}` — `operator.admin`,
accepts a bare record (`server-methods/restart.ts:37-45`), which our scopes already cover.

### B3 · Gateway recovery restarts via `update.run`, the software updater
**Status:** TODO — collapses into B2's helper. **Class:** MISWIRED. **Effort:** small.
`update.run` runs `runGatewayUpdate` with a git target and install-root handoff
(`server-methods/update.ts:204-245`; params `target:{kind:"git", upstreamRef, upstreamSha}`).
It is not a restart primitive. `routers/container_recover.py:218` calls it with no params as
the "gateway is down → restart it" lever, on a container running our pinned ECR image.

---

## P1 — from the gateway RPC lane

### B4 · `chat.history` gained a cursor delta; we hand-roll reconnect catch-up
**Status:** TODO. **Class:** HAND-ROLLED. **Effort:** medium.
New at 8.1: `server-methods/chat-history-delta.ts` (file absent at 7.2). Params gained
`cursor` and `pendingBefore` (`schema/logs-chat.ts:27,31`); the result is now a union —
`{kind:"delta", messages, deltaCursor, sessionInfo, inFlightRun, pendingInputs}` or
`{kind:"reset"}` (`:47-72`). `cursor` is mutually exclusive with `offset`/`messageId`
(`chat-history-handler.ts:208-212`).
Ours: a recursive quick/confirm/wake ladder (`hooks/useAgentChat.ts:996-1010`) that refetches
a whole page on reconnect, using `health` as a liveness probe (`:1096`), plus a module-level
`_historyPaging` cursor map (`:1408-1440`).
`cursor` replaces the **reconnect catch-up**, and `{kind:"reset"}` is the gateway telling us
about the exact discontinuity our ladder guesses at. **Keep `offset`** — `loadOlderMessages`
backward paging still needs it. Pairs with D4 (same response carries `inFlightRun`).

### B5 · We discard the whole `hello-ok`, including `server.bootId`
**Status:** TODO. **Class:** UNUSED. **Effort:** small.
`HelloOkSchema` gained `server.bootId` and `server.buildId` (`schema/frames.ts:82-87`,
set at `server/ws-connection/connect-hello.ts:127-134`), and carries `features.methods`
(the live method list), `features.capabilities` (13 entries), and `policy.attachments.*`.
`core/gateway/operator_rpc.py:228-257` returns `resp`; no caller reads it.
**`bootId` is the valuable one:** a changed `bootId` across a reconnect is the authoritative
"the gateway restarted, your cached run/session state is stale" signal — same class as the
never-cache-a-container-IP rule that already cost us a prod outage. `features.methods` is a
free version-drift assertion.
Note `policy.attachments` does **not** replace `_fit_rpc_result_for_frame`
(`routers/websocket_chat.py:334-374`) — that trim exists for the API Gateway frame limit,
which is our constraint, not the gateway's.

### B6 · `cron.runs` is the server-side answer to the routine-run telemetry gap
**Status:** TODO. **Class:** UNUSED. **Effort:** medium.
`CronRunLogEntrySchema` (`schema/cron.ts:765-800`) carries `status`, `completionStatus`
(new at 8.1), `errorReason`, `durationMs`, `runId`, `sessionKey`,
`usage{input/output/cache tokens}` and a `delivery` trace. `CronRunsParamsSchema` (`:746-762`)
filters server-side: `scope:"all"`, `statuses[]`, `deliveryStatuses[]`, `query`, `sortDir`,
`limit`/`offset`. **We have zero callers** — `cron.runs` appears nowhere in the repo.
*Honest caveat:* most of that filtering existed at 7.2; only `completionStatus`, `delivery`
and `deliverySuppressionReason` are new. A standing unused capability, not an 8.1 gift —
but it is the direct server-side answer to the recorded routine-run telemetry gap, and it
pairs with A8 (`status`/`lastRunError` on the session row).

### B7 · `models.authStatus` is per-agent since 8.1; we always read the default
**Status:** TODO. **Class:** MISWIRED (latent). **Effort:** small.
8.1 added `{refresh?, agentId?}` and routes through `resolveModelAuthAgentScope`
(`server-methods/models-auth-status.ts:443`); 7.2 had no params schema and used
`resolveDefaultAgentDir`. `core/services/spend_service.py:237` passes `{}`, so we read
**Friday's** credentials and label them the owner's.
It does not error today — `resolveDefaultAgentId` resolves the single `default: true` entry
and we emit exactly one (`config.py:1051`). But set `agents.ownership: "explicit"` or drop
the marker and 8.1 raises `AgentSelectionRequiredError` → `INVALID_REQUEST`, which
`spend_service` swallows into a blank card. Worth knowing given the standing
"all spend $0.00" symptom.

### B8 · Smaller RPC-lane items
- **`channels.pairing.list/approve/dismiss`** — a whole new 8.1 surface
  (`schema/channel-pairing.ts`) for pending DM sender-access requests per channel account.
  We don't touch it. Directly relevant to the Slack platform-app cold-install work. Medium.
- **`chat.send` gained `replyToId`** (`schema/logs-chat.ts:190-192`); `message.action`
  gained a structured reply union (`schema/agent.ts:93-108`). We send no reply context
  (`routers/websocket_chat.py:2542-2545`, `:2809`) and carry threading via the
  `lightbulb-email:` session-key prefix. Take only if threading is currently lossy.
- **`traceparent` on request frames** (`schema/frames.ts:179`, new at 8.1) — adding it to
  `send_rpc` (`connection_pool.py:972-975`) joins our backend traces to container-side spans. Small.
- **Dead `update_plan` display rows** — 8.1 deleted the tool
  (`tools/update-plan-tool.ts` gone, `progress-card-tool.ts` added; kill switch moved
  `tools.experimental.planTool` → `tools.updatePlan`). We already adopted the replacement
  (`hooks/useProgressCard.ts`) but still map `update_plan` in
  `lib/toolDisplay/index.ts:147` and `tool-display.json:435`. Delete the two rows. Trivial.

### B9 · Method-set delta, for the record
**304** core methods at `v2026.7.2-isol8.8` → **393** at `v2026.8.1-isol8.3`.
**Ten removals** — `sessions.unsubscribe`, `sessions.compaction.get`,
`controlUi.sessionPullRequests`, `talk.session.{join,startTurn,endTurn,cancelTurn}`,
`terminal.text`, `voicewake.routing.set`, `doctor.memory.remHarness` — **we call none of them.**
Of the ~99 additions we already use exactly one (`progressCard.get`).

### RPC-lane DROP list — do not "fix" these
- **`session-scoped-events` cap — do NOT declare.** `server-broadcast.ts:309-313`: declaring
  it *narrows* fan-out to explicitly subscribed sessions. Wrong for a backend proxying every
  session an owner has.
- **`usage-refreshing` cap — do NOT declare.** `server-methods/usage.ts:110-120`: with it,
  `usage.status` returns an incomplete cold `refresh-marker` you must converge yourself.
  Our blocking read is the better default.
- **`run-tool-bindings`** — requires a *paired browser-copilot* client
  (`chat-send-request.ts:204-213`, `connect-admission.ts:232-244`). Unreachable.
- **`agents.list` really takes no params** — `closedObject({})`
  (`schema/agents-models-skills.ts:140`). The comment at `admin_service.py:484-488` is accurate.
- **`node.pair.approve` is still `{requestId}` only** (`schema/nodes.ts:75`); the per-command
  grant approximation in `core/services/desktop_pair_live.py:190-215` cannot be tightened.
- **All five error-string matches must stay** — "unknown requestId"/"unknown deviceId" are
  still bare `INVALID_REQUEST` messages; `agents.create`'s structured
  `createError("already-exists", …)` is flattened by the handler. 8.1's new
  `GatewayErrorDetailCodes` covers none of ours.
- **No native channel-ready signal** — the 2s/390s `channels.status` poll in
  `lib/awaitChannelReady.ts:76-104` stays.

---

## P1 — from the UI/UX lane

Upstream has **two** native implementations, not three: iOS and macOS share one SwiftUI
tree (`apps/shared/OpenClawKit/Sources/OpenClawChatUI/`), Android reimplements it in Compose
to deliberate parity. There is no 2026.8.0 — 7.2 → 8.1 is one release.

**The lane's one-sentence version:** upstream answers *"what is my agent doing, how is it
going, how long has it been going"* with a model-written sentence, a health verdict and a
ticking clock, everywhere a session appears. We answer it with the word **"Working…"**.
That single gap generates about half of what follows.

### UI0 · A ticking clock inside `aria-live="polite"` during provisioning
**Status:** TODO. **Class:** live a11y bug. **Effort:** XS.
VERIFIED. `ProvisioningStepper.tsx:708` runs `setInterval(tick, 1000)` and `:1780` renders
that clock inside `aria-live="polite"`. Provisioning routinely runs 5+ minutes. A screen
reader is read the elapsed time **once per second for the whole of onboarding**, drowning
out the real step transitions in the same region.
Upstream sets **`aria-live="off"`** on six in-flight surfaces for exactly this reason, and
writes the reason down in two files: `chat-working-indicator.ts:78-79`,
`chat-subagent-activity.ts:139-140`, `chat-swarm-progress.ts:176-178`,
`chat-thread.ts:124-126` (`role="log" aria-live="off"`), `chat-session-rail.ts:573-574`.
**Fix:** drop `aria-live` from the clock; keep it on the step text. **Do this before UI1**
— it is the same mistake we would replicate in chat.

### UI1 · "Working…" carries no liveness signal
**Status:** TODO — **highest ratio in the UI lane.** **Effort:** small.
`StreamingStatusBar.tsx:41` renders the literal `"Working…"`, and only in the silent-gap
case (`:33`). Our filament animation (`globals.css:624-657`) is lovely and says the same
thing at second 3 and second 300.
Upstream's row (`chat-working-indicator.ts:93-121`): decorative glyph (`aria-hidden`),
an elapsed clock that **stops** when `endMs` lands (`components/elapsed-time.ts:14, 25-32`),
a live output-token count once usage exists (`:49-57`) — *"Streaming tokens are the real
liveness signal"* (`:70-71`) — a `waitingApproval` variant that **parks the animation and
drops the clock** (`:85-95`), `role="status" aria-live="off"` with semantics on an `sr-only`
"Working…" (`:78-79, 106`), and a **turn recap** — "Done in {duration} · {n} tokens" —
sticky until the next run (`:129-156`).
Four details that separate an honest clock from a decorative one:
- **Start time is the minimum of every candidate** — cached prior value, stream start,
  queued `createdAt`, every segment `ts`, every tool message receive time
  (`chat-progress.ts:216-230`). It reports the earliest *evidence* of the turn, not when the
  UI noticed. Run identity is `runId ?? segmentRunId ?? toolRunId ?? queuedRunId`, with queue
  identity barred from claiming an active tail (`:209-210`).
- **Named startup phases** before the run begins (`chat-run-startup.ts:13-21`) — pairs
  exactly with **D3**, which is the backend half of the same feature.
- **The recap refuses to guess** — `resolveTurnRecap` (`chat-progress.ts:321-405`) captures a
  baseline, accepts only a row whose `endedAt` moved past it, abandons after a 30s settle
  window, and emits **no recap on a failed run**: *"the error surfaces own those"* (`:389-391`).
- **One work indicator at a time** (`chat-transcript-projection.ts:604-642`): working →
  recap → background-tasks, tasks suppressed while `runWorking`.
Their long-wait filler is a *designed refusal*: silent for **30s**, then rotates every 45s,
and the header states the rule (`components/working-phrase.ts:1-4, 36, 38`) — *"so long quiet
runs feel alive **without claiming progress data the UI does not have**."*
**Take:** elapsed time is the whole win and it is free — we already have `startedAt` on tool
calls (`ToolActivityTrace.tsx:43`) and run start in `useAgentChat`. Token counts need **D8**.
**Skip** the crab-gerund word list; if we want a long-wait phrase it must be in our voice,
and the 30s-silence rule is the part to copy.

### UI2 · Nothing outside the tab you are looking at tells you anything
**Status:** TODO. **Effort:** small (roster badge) / medium (dock tabs).
Three confirmations of one hole: dock tabs carry no status field (`panelRegistry.ts`);
approvals mount in exactly one place (`AgentChatWindow.tsx:564`) even though the queue is
already a **global** context (`hooks/useExecApprovals.tsx:388`); and
`grep -rni "unread" components/{chat,workspace,agents}` → **zero hits**.
Net effect: Atlas blocks on an approval while you read Friday, and you find out from a
transient toast (`RunNotifications.tsx:135-163`) or not at all.
Upstream layers it: a running ring on the leading glyph (`session-glyph.ts:23-29`; CSS
`components.css:163-171`, with an explicit reduced-motion branch freezing it solid at
`:5935-5940`), an unread dot suppressed while running
(`session-leading-indicator.ts:75`), a trailing state glyph
(`session-attention-presentation.ts:57-102`), a badge row — approval shield, outbox
attention **with a count**, unsent-draft pencil, PR state, incognito lock
(`session-row-badges.ts:59-190`) — and a new sidebar **attention inbox**
(`sidebar-attention-entries.ts:41-50`) that replaced the deleted `update-banner.ts` and
`connection-banner.ts`.
**The rule to copy is from native: badges roll up the tree.**
`ChatSessionSidebarModel.swift:161-175` sums children into the parent (`:171-174`), so a
collapsed row still shows a child working.
**Judgement:** the full inbox is more machinery than we need; the row-level part is not, and
we already hold the state — `useRosterEntries.ts:82-122` already unions Teams roster status
with live `run_active` frames.

### UI3 · No way back to the bottom of a long transcript
**Status:** TODO. **Effort:** small.
`scrollToBottom` exists only as an imperative handle (`MessageList.tsx:1183-1238`), called
once 50ms after a send (`AgentChatWindow.tsx:406`). Scroll up mid-stream and the only way
back is manual scrolling through a transcript still growing.
Our autoscroll pinning is correct (120px threshold, `MessageList.tsx:1258, 1302-1335`,
rAF-coalesced), so we have the geometry input.
**The gating is the part that matters, and upstream has the scar:** `ChatView.swift:38-49`
records that `hasNewerContentBelow` is **structurally true from the first Writing tick of
every turn**, so gating on it alone made the button *flash on every reply* — issue **#108693**.
The shipped predicate also requires `!isAtLiveEdge` from real scroll geometry (`:407-417`).
Second trap, same file (`:51-63`): release follow on `.interacting` **and `.animating`**, or
system scrolls (status-bar tap, keyboard avoidance) yank the reader back mid-read.
Android reserves a 56dp bottom strip so the button never covers content
(`ui/chat/ChatScreen.kt:218-224`).

### UI4 · Session rows show a label; upstream's show a state
**Status:** TODO — folds into **S2/S3**. **Effort:** small.
`SessionList.tsx:104` renders `s.label`. That is the entire row. No timestamp, no preview,
no state; empty state is a *disabled* placeholder reading "New session" (`:68-73`).
**Our own Expo app already does better** — `mobile/src/components/chat/SessionsSheet.tsx:88-100`
shows label + relative time + a one-line preview. Web is behind our own mobile app.
Two craft rules worth taking even without the rest:
- **`SIDEBAR_SUBTITLE_MIN_DISPLAY_MS = 2000`** (`app-sidebar-session-projection.ts:20`) — a
  subtitle holds 2s before replacement, *"without a floor the line swaps A→B→A within a
  second"* — bypassed for operator-critical lines (`:63-69`).
- **Live narration is bounded** — ≤6 background subscriptions, 2s throttle, 16KB buffer
  (`app-sidebar-session-narration.ts:32-34`); the line is the newest sentence of the newest
  paragraph, code fences stripped, clamped to 120 chars (`sidebar-narration-line.ts:4-24`).

### UI5 · Approvals: no clock, no plain-language scope, no global surface
**Status:** TODO. **Effort:** small (countdown) / medium (scope sentence).
Our `ExecApprovalCards.tsx` header says it outright: *"No modal, no countdown"* (`:6`). We do
have `<mark>`-highlighted risky spans (`:29, 67`) and a good "Trust will always allow…"
explainer (`:149-150`).
Upstream's card adds a **live countdown to expiry**, ticking 1/sec, mirrored into the modal's
`description` for screen readers (`exec-approval-card.ts:51-85`), `--urgent` under 2 minutes
(`:264`); and **`summarizeScopeLabel`** (`:150-173`), which turns the typed scope into a
sentence — a standing grant with expiry in days, a message-send with recipient count and
target, **a payment with amount, currency and target**, an external post with its target.
Also a second surface, `renderSidebarApprovalRow` (`:255+`), reachable from anywhere with a
`queueCount` for what is waiting behind it.
**The scope sentence is the one to fight for** and the one with a plumbing dependency (the
typed scope must reach us). The countdown is cheap and worth it — an approval that silently
expires while the owner is in another tab is bad regardless of audience. **Skip** the
keyboard shortcuts; that is developer ergonomics.
Note this interacts with **B1**: our plugin/system-agent approvals currently expire instantly
with `decision: null`, so fix B1 first or there is nothing to count down.

### UI6 · The session observer — reconciled cost, and what it actually buys
**Status:** TODO — **decision, not a slipped-in PR.** **Effort:** medium (roster line) / large (rail).
Three lanes described the cost three ways. **Adjudicated by reading the code:**

| audience tier | how you get it | what you get | model cost |
|---|---|---|---|
| `none` | no visible subscriber | nothing; observer suspends (`session-observer.ts:454`) | zero |
| **`broad`** | a visible **`sessions.subscribe`** subscriber — **what we would have** | **headline only**, from the preamble publisher | **zero** |
| `direct` | a visible **`sessions.messages.subscribe`** subscriber | headline + `health` + `assessment` + `planProgress` | utility model |

`classify()` (`session-observer-audience.ts:40-53`) returns `"direct"` only for a message
subscriber; `utilityModelRef = ... currentAudience !== "direct" ? undefined : modelSlots.claim(...)`
(`session-observer.ts:669-672`). And the preamble publisher
(`session-observer-preamble.ts:53-60`) emits `headline` plus a `health` **carried forward
from a previous digest** — it does not compute one. At `broad`, no previous model digest ever
exists, so **`health` is never populated.**
**So: the free tier gives us a headline (the agent's own commentary text, normalized). The
`health` verdict — which the UI lane rightly called the part to defend hardest, because
`stuck` and `waiting-on-user` are the two states an owner must be told about — requires the
model tier.**
The digest schema (`packages/gateway-protocol/src/schema/sessions.ts:91-101`): `headline`
1..120, `assessment` 1..320, `health ∈ {on-track, grinding, stuck, waiting-on-user,
wrapping-up, done, failed}`, `planProgress {completed,total}`. The system prompt is explicit
(`session-observer-model.ts:231-237`): *"Do not transcribe the activity log. Summarize what it
is doing and how it is going."*
Model-tier costs are bounded and knowable: `MAX_TOKENS = 300`, `MIN_DIGEST_INTERVAL_MS = 12_000`,
`MAX_DIGESTS_PER_RUN = 40`, `MAX_CONCURRENT_MODEL_SESSIONS = 6` (`session-observer.ts:48-56`).
One digest drives three surfaces: sidebar subtitle (`session-row-subtitle.ts:59-76`, priority
ladder attention → agent status → observer headline → narration → work subtitle → last reply),
the chat rail (`chat-session-rail.ts`), and the iOS/Android session lists.
Two restraint details to steal verbatim: **auto-expand fires once per run, only on `stuck` or
`waiting-on-user`**, tracked in an `autoExpandedRunIds` Set so flapping health never
re-interrupts (`chat-session-rail.ts:107-111`); and **no `aria-live` on the expanded rail**
(`:573-574`) — same rule as UI0.
**Two product decisions if we go to the model tier:** it draws against the $3.00/day trial cap
through the bedrock-gate, so gate it to paid; and a tiny model writes user-visible copy —
`sanitizeSessionObserverModelText` (`session-observer-model.ts:418-421`) truncates and strips
markdown, but the sentence is still model-authored on a brand surface.
**Recommendation:** take the free headline first (one RPC, see **A2**), ship a roster line,
and let it earn the model tier.

### UI7 · `ChatErrorCard`'s Retry button can never render
**Status:** TODO. **Class:** bug. **Effort:** XS.
`ChatErrorCard.tsx:326-336` renders Retry gated on `Boolean(onRetry)` (`:293`) — and the only
caller (`MessageList.tsx:704-710`) passes `message`, `code`, `interactive` and **never
`onRetry`**. `MONEY_CODES` (`:84-89`) would suppress it for four of the nine states anyway.
Either wire it or delete it.

### UI8 · Mobile — no haptics, no Live Activity, tool rendering a generation behind
**Status:** HOLD — gated on shipping the app (`co.isol8.mobile` still isn't created).
- **Haptics, S, cheapest "feels real" win.** `ChatHaptics.swift:8-12` fires on exactly three
  events ever: `messageSent` (light), `runCompleted` (success), `runFailed` (error), macOS a
  documented no-op (`:42`); every call site is in the run lifecycle
  (`ChatViewModel+Sending.swift:662,717,762`). The restraint IS the design. We have zero —
  `expo-haptics` isn't a dependency.
- **Live Activity / Dynamic Island, M.** `OpenClawActivityAttributes.swift:10-23` — 12 states,
  three of which mean *you* are the blocker (`approvalNeeded`, `actionRequired`, `attention`);
  an **arbiter** so a low-priority update can't hide urgent work
  (`LiveActivityManager.swift:6-7`); four renderings (`OpenClawLiveActivity.swift:12-45`).
  Schema note worth keeping: they carry a legacy-boolean decoder because *"Live Activities can
  outlive an app update"* (`:79-89`).
- **Tool rendering, M.** `mobile/src/components/chat/ToolPill.tsx:38` uses `label = tool` —
  **the raw tool name**. The `ToolActivityTrace` redesign was never ported to Expo.
- **Do not over-read the pagination gap:** no OpenClaw native client has pagination or
  virtualization either (iOS `IOSGatewayChatTransport.swift:632-634` passes neither `limit`
  nor `maxChars`; Android `ChatController.kt:4076-4079`). Only the Control UI got
  virtualization this release. Fix it because consumer chat apps are expected to have it,
  not because there is a pattern to copy.

### UI9 · Empty state: ours is chrome, theirs is the agent talking
**Status:** TODO. **Effort:** small.
`AgentChatWindow.tsx:492-529` — LogoMark hero, agent name, *"Start a conversation with your
agent"*. No suggestions, no recents.
iOS renders the greeting *"as a grey assistant bubble so the greeting reads like the agent's
first message"* (`ChatProTab.swift:218`, `ChatView.swift:1345-1346`), then starters that send
immediately (`:498-506`). Android's three are **business-shaped, not developer-shaped** —
*"Catch me up" / "Plan the work" / "Use this phone"*, each with a subtitle, shown **only when
connected** (`ui/chat/ChatScreen.kt:1721-1741, 1649-1651`).
**Take the suggestions, in our voice, business-shaped. Skip** their 8-variant skeleton system
— over-built for our 5 panel kinds, and ours is already good (see the keep list).

### UI10 · The 769–1100px dead zone
**Status:** TODO. **Effort:** medium (shell) / XS (transcript column).
`grep -n "@media" components/workspace/workspace.css` → **none**. `workspace.css:23-33` is a
hard `grid-template-columns: 280px 1fr`. The only breakpoint in the shell is a binary swap at
`hooks/useIsDesktop.ts:5` — `(min-width: 769px)` picks `WorkspaceShell` or `MobileChatShell`
(`app/chat/page.tsx:77`), **remounting the other**. At 800px — half-screen browser, iPad
landscape, small laptop — the owner gets the full desktop shell with a fixed 280px sidebar and
zero adaptation.
Upstream uses three narrow definitions, each owned by what it affects: **1099px** collapses
split view (`chat-page.ts:109`); **1100/600px** turns the sidebar into a drawer
(`mobile-nav-layout.ts:5-18, 43-45`); **680px *pane* width** strips the side panel's dock
controls (`sidebar-layout.ts:25, 226-228`). Plus container queries where the container is what
matters, and `@media (hover: hover) and (pointer: fine)` guarding every hover-only affordance.
Their three uncoordinated breakpoints with no shared constant are themselves a smell — **but
three imperfect breakpoints beat one binary swap**, and container queries are the right idea
for a panel shell. We already use `@container` elsewhere (`ProvisioningStepper.tsx:2350`,
`InlineTeamsPanel.tsx:74`, `workspace.css:129`) — just not on the shell.
**Do the XS half immediately:** cap the transcript at ~800px. `ChatView.swift:188-193` does;
our chat panel stretches to the panel, and at 2560px that is an unreadable line length.

### UI11 · Our command palette doesn't know about chat
**Status:** TODO. **Effort:** small.
*Correction to an easy assumption: we DO have a global ⌘K palette* —
`teams/command-palette/CommandPaletteProvider.tsx:33-48`, mounted at `WorkspaceShell.tsx:428`,
searching agents, issues and projects (`CommandPalette.tsx:72-81`).
Deltas: upstream's also searches **sessions and transcripts** (50ms debounce, min 2 chars,
≤4 pages, ≤200 keys, 30s catalog cache — `command-palette.ts:46-54`), runs slash commands and
toggles panels (`:91-111`), and **surfaces degraded search honestly** rather than hiding it
(`searchPartial`/`searchIncomplete`/`searchFailed`, `:222-241`). Ours is also **desktop-only**
— the provider wraps `WorkspaceShell` but not `MobileChatShell`.
We already have transcript search (`SessionSearch.tsx`) — which **S1 removes from the
sidebar**, so routing it into the palette is the natural home for it. **Skip** the shortcuts
cheatsheet; developer-tool furniture.
Two upstream defects **not** to copy: their overlay advertises `⌘ Click` for multi-select
while the handler returns early on ⌘/Ctrl and binds `altKey`
(`app-sidebar-session-navigation.ts:428-439`); and bare single-character keydown focuses the
composer from anywhere in the pane, capture-phase (`chat-pane-lifecycle.ts:356-376`).

### UI12 · The transcript has no live region at all
**Status:** TODO. **Effort:** small. *(Paired half of UI0.)*
`MessageList.tsx` has zero live regions; all 11 `aria-live` hits in `components/chat/*.tsx`
are in `ProvisioningStepper.tsx`. A screen-reader user gets **no announcement when their agent
replies**.
Upstream funnels everything through one polite region
(`chat-transcript-announcement.ts`), and its editorial rules are the good part: a completed run
announces its `actionOwner` message — the *final answer*, not the last thing that happened
(`:71-76`); a **failed run announces nothing** (`:94-96`); mounting a thread never dumps the
last message in (`:141-149`); capped at 500 chars, UTF-16-safe (`:16, 61-64`); flips to
`aria-live="off"` when the pane is backgrounded (`chat-pane-retained-presentation.ts:97`).

### UI13 · Subagent work is one grey line that says "Delegated"
**Status:** HOLD — plumbing-gated on **D6** (`spawnedBy`). **Effort:** medium.
A spawned subagent renders as one tool-trace row reading "Delegated", from three verb entries
in the vendored map (`lib/toolDisplay/index.ts:141-143, 364`). Its work is invisible until it
returns.
Upstream: up to 5 rows, active ordered ahead of completed, **completed rows retained 60s**
before expiring, and an overflow count of how many more are still working
(`chat-subagent-activity.ts:10, 11, 56-62`); each row has an indicator, a status word, and a
snippet falling back `terminalSummary` → `lastActivity` → `progressSummary` → `lastToolName`
(`:84-112`).
**Ranked lower than its novelty suggests** — our owners mostly run Friday and Atlas, not
fan-outs. **The 60s terminal retention is the transferable bit:** a child that finishes must
stay on screen long enough to be read.

### UI14 · Errors don't say whose problem it is
**Status:** TODO. **Effort:** small.
`ChatErrorCard.tsx` has nine well-typed states — `rate_limit` (`:93`), `context_length`
(`:113`), `timeout` (`:121`), `refusal` (`:128`), `trial_daily_cap` (`:135`), `trial_expired`
(`:155`), `out_of_credits` (`:166`), `subscription_inactive` (`:176`), unknown (`:187`) — each
with title, description and at most one action. No "whose problem is this" line.
`GatewayProblemView.swift:105-111` renders **"Fix on gateway"** vs **"Fix on this device"**,
with icon and tint derived from failure class (`:69-103`). For an owner, *"this is on our end"*
vs *"this is on yours"* is the most useful sentence on the card. It is a field on states that
already exist.

### UI-next · Not audited, flagged honestly
**QuestionCards.** Upstream's (`chat-question-card.ts`, 728 lines) is a docked
one-question-at-a-time wizard: `n/m` progress, number-key shortcuts, roving radio-group focus
that navigates *without* auto-advancing while clicks and number keys do (`:401-446`), a
secret-store block typing input as `password`, and five terminal states including **"Answered
elsewhere"** (`:126-147`) — so answering on your phone visibly resolves the card on your laptop.
Native adds a live expiry countdown (`ChatQuestionCard.swift:78-119`). Ours is
`QuestionCards.tsx`, 285 lines, not compared line by line.
**For a product where the agent asking the owner a question is a core loop, this is the most
likely place for another finding the size of UI6.** Look here next.

### UI-keep · Verified as good or better than upstream — do not spend time here
1. **Tool output.** `ToolActivityTrace.tsx` is better than upstream's tool cards *for our
   users*: plain-language labels from a vendored map (`lib/toolDisplay/index.ts:302-349`) —
   "Reading lines 40-79 from connection_pool.py", not `mcp__openclaw__web_search`; success is
   **uncoloured** (`:22-26`) so the only coloured thing in a healthy turn is nothing; folds to
   one summary line above 3 calls; and a **90-second stall guard** (`:52-58`) converting a
   running row to "outcome unknown" rather than spinning forever — **upstream has no
   equivalent.** Their own tool payloads (unified diffs with line numbers, 40-line truncation)
   are explicitly what a business owner should never see.
2. **Roster status honesty.** `AgentRoster.tsx:120-155` refuses to guess — an unrecognised
   status gets its own `unknown` state and the raw value shows verbatim, reasoning written
   down (*"both obvious answers are lies"*). Upstream's row states are a fixed icon set with no
   such valve.
3. **Typed error states** — nine, with the two trial codes swapping the red card for an inline
   purchase form, and only the newest interactive (`MessageList.tsx:1365-1376`). Upstream has
   nothing product-specific here; they don't sell anything.
4. **Reconnect + outbox** — composer stays enabled, sends queue, amber row explains it,
   per-row Retry (`ChatInput.tsx:494-499, 608-618`), `waiting-reconnect` drains on the rising
   edge (`useAgentChat.ts:2646-2691`). The **idle-park** state — *"Agents asleep — they wake
   when you interact"* (`useSystemHealth.ts:165-166`) — is ours alone and exactly right for a
   scale-to-zero trial container.
5. **Don't claim work while disconnected** — `containerRunActive` is force-cleared on
   disconnect (`useAgentChat.ts:2704`). Upstream states the rule: *"Disconnected snapshots are
   stale… a ticking 'running' claim would be a lie"* (`chat-background-tasks-status.ts:104-106`).
   **Load-bearing — don't let anyone "fix" it.**
6. **Parked abort replayed on reconnect** (`useAgentChat.ts:2646-2691`) — parity with
   `run-lifecycle.ts:312-363`.
7. **Optimistic send with identity-keyed rollback** (`useAgentChat.ts:2242-2275`).
8. **Command spans** — `<mark>`-highlighted risky substrings already ported.
9. **One glyph, several postures — no generic spinner in the thread.** Our lamp/filament
   (`globals.css:624-657`) is the same discipline, executed at least as well.
10. **Verb tense inflected by outcome** — `resolveToolDisplay` returns `{gerund, past}`. Parity.
11. **Reduced motion** — global floor at `globals.css:912-929`. One nuance where they're
    ahead: their running-row shimmer is **opt-in** via
    `@media (prefers-reduced-motion: no-preference)` (`styles/chat/tool-cards.css:311`), so
    reduced-motion users keep static text *and* syntax colours; ours strips slightly more than
    it needs to.
12. **Panel dedup** — `addOrFocus` (`workspaceController.ts:168-208`).
13. **Progress card** — `AgentProgressCard.tsx` is substantive parity with
    `session-progress-card.ts`. Only delta is their **responsive placement**
    (`board | composer | hovercard`). Nice, not urgent.
14. **Our history skeleton** — content-shaped, staggered delays, `motion-reduce:animate-none`,
    and it renders a *disabled ChatInput underneath* so the composer doesn't pop in
    (`AgentChatWindow.tsx:436-477`). **No native OpenClaw client has skeletons at all.**
15. **Things upstream's natives simply lack**, so there is no pattern to copy: message
    grouping, date separators, per-message timestamps on Apple, swipe actions on session rows,
    pull-to-refresh, and queueing a message while the agent runs.

### UI-retired · Patterns of ours upstream deliberately moved on from
Verified in the diff, not inferred from changelog wording.
- **Interruption banners → permanent triageable chrome.** They deleted
  `connection-banner.ts` and `update-banner.ts`, replacing them with a persistent
  low-salience footer chip that distinguishes `offline` vs `restarting`, surfaces a
  **queued-outbox count**, and is its own retry button (`session-row-badges.ts:193-228`,
  mounted `app-sidebar-render.ts:371-380`). Ours are still inline
  (`AgentChatWindow.tsx:552`, `ChatInput.tsx:494-499`).
- **Global panel visibility → per-session layout.** They ran a one-shot migration retiring
  exactly our model: `sidebar-layout-legacy-migration.ts:50-54` — *"Move the shipped global
  Browser/Desktop visibility into the current session once. A marker prevents that global
  preference from opening unrelated future sessions."* Layout is now per session, LRU-capped
  at 50 (`sidebar-layout-persistence.ts:8`). Ours is one blob per profile
  (`layoutStorage.ts:23`, key `isol8.workspace.layout.v3`). **Know this before investing
  further in the single-blob model.**
- **Ephemeral plan checklists → durable progress card.** We already made this move
  (`AgentProgressCard.tsx:5-8`). Noted so nobody re-litigates it.
- **String-encoded side chat — a road not to take.** They deleted `chat-side-chat.ts`, whose
  transport embedded prior context into a `/btw` string that *"is never parsed back apart"*,
  fixing a leak where private companion wrappers appeared as answers (#122380).
- **Many destinations → few.** Android retired Voice as a destination and moved it into the
  composer (`ui/ShellNavigation.kt:111-112, 20`); iOS root nav is one black sidebar on every
  idiom (`apps/ios/DESIGN.md:8`), `RootTabs` a fossil name with no `TabView` anywhere. **Both
  are the move we already made in `572956fa0`.** Android ships five sidebar destinations
  against iOS's seventeen — the consumer-shaped one is the short list.

### UI-no · Explicitly not recommended
Crab-gerund phrase lists; the 8-variant skeleton system; a keyboard-shortcuts cheatsheet; the
5-layer Escape cascade; bare-keystroke autotype; the split tiling tree; and the two competing
session grouping/sort vocabularies. Developer-tool furniture — and two of them are defects
upstream hasn't noticed yet.

---

## P0/P1 — from the multiplayer & identity lane

**The one fact that decides this whole lane:** our gateway connection has **no human
identity, and cannot have one.** Every 8.1 collaboration feature keys off
`client.authenticatedUserProfile`, assigned once at connect from the transport's own
authentication. We set `auth.mode: "token"` (`config.py:1562-1565`) and connect with
`auth:{token}` (`operator_rpc.py:207-224`). `GatewayAuthResult.user` is set in exactly two
places — `auth.ts:493` (trusted-proxy) and `:546` (tailscale). **Token auth never sets it.**
So `connect-session.ts:208-232` never runs, the profile stays `undefined`,
`gatewayClientSessionCreator()` returns `undefined`, and every downstream feature degrades to
"one anonymous admin." No `createdActor`, no transcript `sender`, no presence identity,
nothing for person filters to match, no role policy.

### G-F1 · Vendor upstream's session-target registry — folded into SEC-1
**Status:** TODO — see SEC-1. **Effort:** ~1 day. **Highest value/effort in this lane.**

### G-F3 · Admin methods with no session key — the registry structurally cannot cover these
**Status:** TODO. **Class:** SECURITY. **Effort:** 1–2 days.
Reachable through the proxy from any org member's browser, carrying no session key:

| method | scope | note |
|---|---|---|
| `secrets.store.list` / `.set` / `.delete` | `operator.admin` | `core-descriptors.ts:573-575`, new in 2026.8 |
| `terminal.open` / `.attach` / `.input` / `.list` / `.close` | `operator.admin` | `:390-393, 434-435` |
| `users.setRole`, `users.linkEmail` | `operator.admin` | `:194, 197` |
| `users.list` | `operator.read` | `:192` — returns every profile **with all emails**, no per-caller filter |
| `config.set`/`.patch`, `skills.install`, `cron.*` | admin | — |

**Fix:** a default-deny method allowlist in the proxy. That is the correct shape regardless —
the forward-anything posture is what makes every finding in this lane possible.
**Mitigating fact, verified:** our own clients call none of these —
`grep -rE "session\.(members|suggestions|visibility|typing|discussion)|secrets\.store|terminal\.(open|attach)|users\.setRole"`
across frontend, shared and mobile → **0 hits**. Exploitation needs a hand-crafted WS frame
from an authenticated org member, not an accidental click.

### G-F4 · `permissionMode: "full"` self-escalation
**Status:** TODO. **Class:** SECURITY. **Effort:** half a day.
VERIFIED at the tag. `sessions.patch {permissionMode:"full"}` requires `operator.admin`
(`src/shared/session-method-scopes-base.ts:38-46`) — which **our proxy supplies**. The effect
(`src/agents/session-permission-exec-mode.ts:9-35`): `full` → `execMode: "full"`,
`workspaceOnly: false`, `applyPatchWorkspaceOnly: false`, `bypassHostApprovalFloors: true`.
A member patches **their own** session, so every ownership check passes, and gets exec outside
the workspace with approval floors bypassed — i.e. read access to every other member's
`workspace-*` on the shared EFS mount, and to the container env (which is where the BYO key
lives, see G-F5).
**Not an 8.1 regression** — 7.2 had the same admin bar on the equivalent field
(`v2026.7.2-isol8.8:src/gateway/method-scopes.ts:83-100`); 8.1 renamed
`execSecurity`/`execAsk` → `permissionMode` and made the gate explicit. Pre-existing,
unchanged, still reachable.
**Needs a params-level check, not a key check.**

### G-F2 · Operator roles are CEREMONY for us — and half-fixing them makes it worse
**Status:** DROP for now; revisit only after G-F6. **Effort:** n/a.
Upstream's roles are real machinery — ~40 enforcement call sites including the universal
pre-handler fence `resolveSessionMutationAuthorization` (`server-methods.ts:501-509` →
`session-sharing.ts:382-618`). They are unreachable for us for **two independent** reasons:
- **(a) Roles never bind to a shared-secret operator.** `connect-session.ts:206-207, 243-249`:
  `sharedSecretOperatorOwner = role === "operator" && (authMethod === "token" || "password")`,
  and `rolePolicy` is only resolved when that is false. Ours is always true, so setting
  `gateway.roles` in `openclaw.json` is a **no-op** — inert, not broken.
- **(b) `operator.admin` short-circuits every consumer anyway.** `isGatewayAdmin` is literally
  the scope check (`session-sharing.ts:93-97`) and wins first in each place that matters:
  `resolveSessionSharingRole` → `"admin"` (`:167`); `createSessionListEntryFilter` →
  `undefined`, i.e. **no filter at all** (`:723`); `canReceiveSessionEvent` → `true` (`:656`);
  `resolveSessionCatalogVisibility` → `"unrestricted"` (`session-catalog-visibility.ts:48-50`).
  We hold `operator.admin` by design (`operator_device.py:85-92` — `skills.install`,
  `agents.create`, `cron.*` all require it).

> **⚠️ THE TRAP:** fixing only (b) is **worse than doing nothing**. Drop admin while keeping
> `auth.mode: "token"` and `rolePolicy` is still `undefined`, so no filtering is gained —
> while `resolveSessionSharingRole` with no identity and roles configured returns
> **`"viewer"`** (`session-sharing.ts:172-178`). **Every org member becomes read-only.**
> Both changes land together or neither.

Upstream's own doctrine, worth quoting when this comes up again —
`docs/concepts/multi-user.md:14`: *"Session ownership, visibility in the sidebar, and presence
indicators are usability features, not security boundaries."* **Our backend stays the real
boundary regardless.**

### G-F5 · BYO LLM keys are plaintext in the container environment
**Status:** TODO — worthwhile, but call it depth, not a boundary. **Effort:** 3–5 days.
`core/containers/ecs_manager.py:2592` injects the owner's key as an ECS task-def secret named
`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. Any member who reaches exec — trivially via G-F4, or
ordinary agent tool use — reads it with `echo $ANTHROPIC_API_KEY`.
8.1's store is a real improvement and the plumbing exists:
`models.providers.<p>.apiKey?: SecretInput` (`types.models.ts:217`), `SecretInput = string |
SecretRef` with the new `"store"` source (`types.secrets.ts:7, 17-24`), resolved by
`secrets/resolve-store.ts:10-61`. With a `kind:"secret"` row and the egress proxy off,
secret-kind entries are **not placed in the exec environment at all**
(`secret-store.ts:273-333`).
**Be precise about what it buys.** Store values are plaintext in `openclaw.sqlite`
(`secret-store.ts:381-447`), protected only by best-effort `0o600`/`0o700`
(`openclaw-state-db-permissions.ts:13-34`), and the exec guard only rejects a literal
`sqlite3` argv[0] (`exec-control-command-guard.ts:160-161`) — `python3 -c "import sqlite3…"`
walks straight through. This moves the key from *"visible in `env`"* to *"requires reading a
SQLite file"*. Real reduction in casual exposure; **not a boundary.** Scope is per-gateway
only: `normalizeScope()` hardcodes `{scopeKind:"team", scopeId:""}` (`secret-store.ts:48, 78-80`).

### G-F8 · Shared sessions for Teams — a real product, and it does NOT need identity
**Status:** TODO if Teams demand is real; independent of everything else. **Effort:** ~1 week.
Upstream's participation model is inert on our container (everyone resolves to `"admin"`,
G-F2b), but the **state** it maintains is still readable and `sessions.list` already projects
it: `schema/sessions-row.ts:140-145` carries `createdActor`, `owner`, `visibility`,
`sharingRole`; values are `shared | read-only | suggest | draft`
(`schema/sessions-sharing-values.ts:4-11`).
Since **our proxy is already the enforcement point**, the lazy path is: keep the session key
as ownership truth, let the container hold `visibility` as a flag, and relax
`_filter_sessions_for_user` (`websocket_chat.py:128-144`) to pass rows whose visibility is
`shared`/`read-only`/`suggest` — gating writes in **our** guard rather than the container's.
Several people watching and steering one agent run, with our backend deciding who may steer.
What you don't get without G-F6 is *who* is watching — no presence, avatars or typing
indicators, because there are no identities to show.

### G-F6 · Per-member identity — 2–4 weeks, and we already left this road once
**Status:** HOLD. **Effort:** 2–4 weeks. Prerequisite for G-F2 and G-F7.
Only trusted-proxy and Tailscale mint a profile. Trusted-proxy is viable:
`gateway.auth.mode:"trusted-proxy"` + `trustedProxy.userHeader` + a `gateway.trustedProxies`
allowlist (`types.gateway.ts:185-208`, `auth.ts:202-254`); each distinct header value gets a
profile via `ensureProfileForEmail` (`state/user-profiles.ts:313`).
The profile binds **at connect**, and `ConnectParamsSchema` is a closed object with no
identity field (`schema/frames.ts:30-75`) — so **one connection per member is not an
optimization, it is the only mechanism.** No wire-settable override exists
(`internal.senderAttribution` moves transcript labels only, one in-process path).
Four things to price it with:
1. **We already ran this and rolled it back** — `config.py:1611-1619` records commit
   `ae2ca6086` (2026-03-24) setting `auth.mode = "trusted-proxy"` with `x-forwarded-user`,
   replaced by the signed-device handshake. The reasons predate profiles existing.
2. **Device-token auth survives trusted-proxy mode** (`auth-context.ts:300-303` runs the
   device path only when `!authOk`), so the control-plane connection keeps its credential
   while member connections use the header. They coexist.
3. **Pre-auth budget: 32 concurrent handshakes per client IP**
   (`server/preauth-connection-budget.ts:8`), all our sockets sharing one backend ENI.
   Released once the handshake completes, so it is a reconnect-storm ceiling, not steady
   state; overridable via `OPENCLAW_MAX_PREAUTH_CONNECTIONS_PER_IP`.
4. **Pool re-keying is the real work** — `_connections`, `_frontend_connections`,
   `_pending_rpcs`, `_active_runs`, `_run_state` are all owner-keyed
   (`connection_pool.py:2659-2660`).

### G-F7 · Per-requester MCP OAuth — UNREACHABLE, and would be worse if enabled
**Status:** DROP. Do not adopt without G-F6.
The requester key is `sha256(messageChannel ‖ agentAccountId ‖ requesterSenderId)`
(`mcp-oauth-identity.ts:32-38`), and `requesterSenderId` traces to `client.connect.client.id`
— a fixed enum sent once at connect (`client-info.ts:14-33`); ours is the constant
`"gateway-client"`.
We don't configure shared MCP OAuth today, so **nothing leaks now**. If we enabled it, every
org member would share one OAuth token and one upstream account — and the consent URL surfaces
*inside the agent's chat reply* (`agent-bundle-mcp-requester-connect.ts:50-62`), so whichever
member clicks it authorizes **their personal account for the whole org**. Worse than not
having the feature.

### G-note · One expectation to correct about identity
Profiles would **not** give us an audit trail. OpenClaw's audit store has no human actor type
— `actorType` is `"agent" | "system" | "channel_sender"` only (`audit/audit-event-types.ts:67`,
`audit-event-store.ts:200, 312`). "Who asked the agent for what" lives in session
`createdActor` and transcript `sender`, not in `audit.*`. **If an owner needs a real audit
trail, that is ours to build regardless — and we already have the raw material in the session
key.**

### G-keep · Checked and genuinely fine
- **Presence exclusion is correct** — `client.mode = "backend"` matches
  `isEphemeralGatewayClient` (`utils/message-channel.ts:52-58`), so we're excluded from
  presence tracking (`connect-session.ts:184-194`). No cross-member presence leak, because
  there is no presence.
- **Member-demuxed event forwarding** — `chat` events and `sessions.changed` route to the
  originating member with an explicit **allow-list** on `webchat` rather than a deny-list
  (`connection_pool.py:~1950, ~2287`). The reasoning ("a malformed key parses to source `""`
  and must NOT fall through") is exactly right.
- **`_user_owns_session_key`** (`websocket_chat.py:100-125`) — the `user_1` vs `user_10`
  boundary check and the case-folding rationale are both correct and non-obvious.
- **`sessions.search`** is guarded on both shapes — list-param pre-check plus
  `_filter_search_hits_for_user` post-filter for the key-less variant.
- **`trustedProxies: ["127.0.0.1","::1"]`** (`config.py:1620`) — correctly narrowed; 8.1's new
  ingress attribution would have turned the old `/8` into a fleet-wide 403.
- **`nodes.browser.mode: "manual"` for orgs** — the same per-member reasoning this lane is
  about, already applied correctly in one place. Treat it as the precedent.

### G-order · The lane's suggested order
1. **G-F1** (vendor the registry) — largest reduction per unit effort, mechanical, pins
   cleanly against the next base bump.
2. **G-F3** (method allowlist) — closes the class G-F1 structurally cannot.
3. **G-F4** (params check on `permissionMode`) — half a day, closes a full-container read.
4. **G-F8** (shared sessions) if Teams demand is real — independent of the rest.
5. **G-F5** (key out of `env`) — worthwhile; be clear internally that it is depth, not a boundary.
6. **G-F6**, and only then **G-F2**/**G-F7**. These are one project, not three, and none pays
   off partially.

---

## P0/P1 — from the capability sweep

Three facts about our deployment shape every verdict, all verified in code:
**the backend can already reach any TCP port on any owner's container**
(`apps/infra/lib/stacks/service-stack.ts:297-304`, `fromPort: 0, toPort: 65535`);
**the gateway binds `0.0.0.0`** (`gateway.bind: "lan"`, `config.py:1593`, → `net.ts:298-300`);
and **every core tool is enabled** (`tools.profile: "full"` + `tools.allow: ["*"]`,
`config.py:1977, 1988`) minus `TOOL_DENY_BASE` and nodes.

### H1 · ~~Tool Search hides the browser~~ — **WRONG. Retracted 2026-09-07.**
**Status:** DROP. **Do not act on the original claim.** Kept in full so nobody re-derives it.
VERIFIED. `resolveToolSearchConfig` (`src/agents/tool-search-config.ts:58,65`) computes
`enabled: resolveBoolean(raw.enabled, configured)` where
`configured = Object.keys(raw).some(key => key !== "enabled")`. We write
`"toolSearch": {"mode": TOOL_SEARCH_MODE}` (`config.py:2074`) with no `enabled` key — **so
Tool Search is ON fleet-wide.** With it on, compaction keeps only the `base-coding` and
`shell` families directly visible (`core-tool-factory-descriptors.ts:92-100`,
`isCoreCodingSurfaceToolName`). **`browser` is not in the model's tool list at all** until it
issues a `tool_search`.

> **WHY THIS IS WRONG — three checks against the tag:**
> 1. **`marketplace_search` is compacted the same way.** `isDirectVisibleCatalogTool`
>    (`src/agents/tool-search-catalog.ts:243-252`) requires `classified.sourceName === "core"`.
>    `marketplace_search` is a plugin tool
>    (`apps/infra/openclaw/plugins/lightbulb-marketplace/src/index.ts:51`), so it is hidden too.
>    She reached it through the compacted catalog **in the same turn** she did not reach
>    `browser`.
> 2. **`browser` was in the prompt by name.** The bounded capability directory is gated on
>    `toolSearchDirectoryEnabled = toolSearchControlsEnabledForRun && toolSearch.catalogRegistered`
>    (`src/agents/embedded-agent-runner/run/attempt.ts:342`) — **not** on mode. It renders in our
>    `tools` mode, listing every catalog tool with a 180-char description
>    (`tool-search-directory.ts:157-197`), sorted by name with a prefix truncation.
> 3. **Reaching a hidden tool is one call**, not search-then-call: `tool_call` takes an exact
>    result id **or name** (`src/agents/tool-search.ts:370`).
>
> **And turning it off would have been actively harmful.** `config.py:2033-2039` records why it
> is on: the catalog is unbounded at config-write time because an owner can connect an MCP
> integration at any moment (`config.py:2580-2582`), and Meta's ads server alone measured **95
> tools / ~172k tokens**. Upstream says direct exposure is right *for small catalogs*
> (`docs/tools/tool-search.md:96-97, 123-125`); ours is not small.
>
> **What survives.** Compaction does suppress upstream's capability-conditioned prompt sections
> (`capabilityToolNames` is computed from the compacted list in `tools` and `code` modes but the
> uncompacted one in `directory` mode — `attempt-tool-search-run-plan.ts:83-85`), including a
> delegation-guidance line that would have told her shell/browser work is delegable. That is an
> argument for `mode: "directory"`, not for disabling the feature, and it is a separate plan.
So the reported failure has **three stacked causes**: the tool isn't visible (H1), SOUL says
the browser needs a paired desktop (F2), and AGENTS says "no agent qualified → check the
marketplace" (F1). Fix all three; H1 is the one nobody would have guessed.
Two things worth knowing:
- **8.1 improved this.** At 7.2 *nothing* stayed visible (`tool-search.ts:40` exempted only
  the control tools); `isCoreCodingSurfaceToolName` is new at 8.1, as is batched discovery
  (#118623) letting the model search several capabilities per request. Both already ours.
- **There is no knob to pin specific tools visible.** `ToolSearchConfig` is
  `{enabled, mode, codeTimeoutMs, searchDefaultLimit, maxSearchLimit}`
  (`tool-search-types.ts:76-82`). And the per-agent slot is a **trap**:
  `agents.entries.*.tools.toolSearch` is schema-accepted (`zod-schema.agent-runtime.ts:998`)
  but `resolveToolSearchConfig` reads `config.tools` off a process-global runtime snapshot
  (`tool-search-config.ts:14-23`), so **"turn it off for Friday only" does not work at this tag.**
**Experiment:** flip `toolSearch.enabled: false` on one dev container and measure browser
reliability. If it fixes it, the trade is "full catalog tokens vs. an agent that uses its
tools" — a product decision, not a config one. Note our own comment at `config.py:2033-2074`
argues the token case well; this is new information against it, not a mistake.

### H2 · Portals — the highest-value new capability, already running and producing dead URLs
**Status:** TODO. **Verdict:** ADOPT. **Effort:** 3–5 days (+ a same-day decision).
The agent calls `portal` with a port; the gateway opens a proxy listener and returns a
tokenized URL, proxying HTTP **and** WebSockets so Vite/Next HMR works
(`docs/gateway/portals.md`; `src/agents/tools/portal-tool.ts:54`;
`src/gateway/portals/portal-service.ts:94-280`; RPCs `portal.list/open/close` at
`core-descriptors.ts:592-594`).
**The tool is already live in every owner's container** — `openclaw-tools.ts:394` creates it
unconditionally for non-sandboxed sessions, and we are not sandboxed. What's missing is the
door: the listener binds an ephemeral port (`portal-service.ts:239`, `port: 0`) and
`summarize()` formats the URL against `httpBindHosts[0]` = `0.0.0.0` → `127.0.0.1:<port>`
(`:88-110`). **So today an agent can open a portal and hands the owner a dead localhost URL.**
Our owners' agents build things and there is no way to look at what they built. The SG rule
already permits it and our backend already holds the required scopes.
**Shape:** it is the browser-view panel again — `routers/browser_view.py:1-31` is the
template (resolve the container IP through Cloud Map per connection, **never cache it**,
relay). Differences: portals need an HTTP reverse proxy as well as a WS relay, and the WS half
(HMR) must ride the `bv-` CloudFront door because the API Gateway HTTP API cannot pass
upgrades. Frontend: one new `PanelKind` (`panelRegistry.ts:6`) rendering an iframe fed by a
`portal.list` poll.
**Same-day regardless of the panel:** portals are on and produce unreachable URLs — either
build the door or deny the tool.

### H3 · `OPENCLAW_SUPERVISOR_MODE=external` — blocks agent self-update
**Status:** TODO — **after B2/B3.** **Effort:** 1 hour.
ECS *is* an external supervisor and we never say so (no `SUPERVISOR` match in
`container-stack.ts` or `config.py`). Setting it (`src/infra/gateway-supervision.ts:5`)
refuses native service install/start/stop/uninstall **and refuses OpenClaw self-update.**
That last is the point: `update.checkOnStart: False` (`config.py:2544`) only stops the boot
check — with `exec.mode: "full"` and the `openclaw` CLI in the image, **an agent can run
`openclaw update` today and replace the runtime under a pinned image.**
**Blocked by B2/B3:** our recovery path calls `update.run`, the real self-updater. Under
external mode that would be refused outright and escalate every recovery to a full
reprovision. Swap the RPC first.

### H4 · `utilityModel` auto-derivation breaks on the ChatGPT OAuth path
**Status:** TODO. **Effort:** 1 hour.
`agents.defaults.utilityModel` unset means "auto" — derive a small model from the primary
provider's manifest for titles and progress narration (`src/agents/utility-model.ts:19-108`).
Exactly two providers declare one: `anthropic` → `claude-haiku-4-5`, `openai` → `gpt-5.6-luna`.
- **Bedrock** (trial + managed) declares none → no change. Correct: a trial owner holds one
  AIP and the gate would refuse anything else.
- **BYO anthropic/openai** → titles and narration silently move to the cheap tier on the
  owner's own key. Free win, no action.
- **ChatGPT OAuth is the risk.** Our path resolves through the bundled `openai` provider
  (`config.py:588`), so auto-derivation hands utility calls `openai/gpt-5.6-luna` **against a
  Codex OAuth token** — exactly the failure class our own `cheap_model_ref` docstring warns
  about (`config.py:915-916`). Degrades rather than crashes (titles and progress cards fail),
  but it is untested.
**Fix:** `agents.defaults.utilityModel: ""` (explicit empty = disabled,
`utility-model.ts:22-28`) on the `chatgpt_oauth` path.

### H5 · Tools exposed with no surface that can render them
**Status:** TODO. **Effort:** 2 hours.
Because we run `profile:"full"` + `allow:["*"]`, these are live in every container with no
renderable surface, so the model can burn owner spend producing artifacts nobody sees:
`suggest_task`, `dismiss_task`, `get_goal`/`create_goal`/`update_goal`, `progress_card`,
`terminal`, `portal`, `github_publish` (`tool-catalog.ts:261-407`).
Two checked and fine: `show_widget` is covered because `canvas` is a policy family mapping to
it (`tool-policy.ts:56-60`) and we deny `canvas`; the `dashboard` deny from #2291 is correct
and separate.
**Either build the surface (portal — H2) or extend `TOOL_DENY_BASE`.**

### H6 · Browser `text` / `requests` / `errors` / `emulate` — ALREADY LIVE
**Status:** none — informational.
The browser action list grew by exactly four entries between the tags
(`extensions/browser/src/browser-tool.schema.ts:36-61` vs 7.2 `:34-55`), all gated on
`profile.driver !== "existing-session"` (`profile-capabilities.ts:41-49`). Our in-container
Chromium is `local-managed`, so **all four are already available on the running image**, and
the tool description self-advertises them (`browser-tool-description.ts:37-52`) — no prompt
work needed. `text` matters most: cheap plain page prose instead of an aria snapshot or a
vision screenshot, a direct token saving on every browsing turn against the $3/day cap.
*Blocked in practice by H1* — the tool isn't visible to begin with.

### H7 · Exec approvals become viable for an autonomous fleet
**Status:** ADOPT-LATER. **Effort:** ~2 weeks, touches the run path.
Today every container runs an **ungated shell** (`tools.exec.mode: "full"`,
`config.py:550-554`), justified because autonomous Paperclip runs have no operator to answer
an approval and the old allowlist deadlocked every routine. **8.1 removes both halves of that
argument:**
- Automation approvals go to **any client declaring the `approvals` capability**, not just the
  Control UI (`docs/tools/exec-approvals.md:440-452`; constant at `client-info.ts:85`). Our
  backend holds a persistent authenticated connection per owner — it can be that client.
  **This is the same capability B1 adds.**
- **Always allow** on an automation mints a scoped standing grant bound to that exact agent,
  job, command, cwd and env, so recurring work stops prompting and fails closed the moment any
  byte changes (`:454-473`). New RPCs `exec.approval.grants.list` / `.revoke`
  (`core-descriptors.ts:120-121`) make the ledger inspectable and revocable.
We already have an owner-facing approval rail in Teams. Work: declare the capability, route to
Teams, mint standing grants on "always", move `exec.mode` off `full`.
Right now a compromised or confused agent has an unrestricted shell in a container holding the
owner's credentials — see G-F4 and G-F5.

### H8 · Secret egress proxy — depth, not a fence
**Status:** ADOPT-LATER, sequenced after H7. **Effort:** 2–3 days.
`secrets.egressProxy.enabled` (default off, `docs/gateway/secrets.md:318-380`) puts an
authenticated sentinel in the subprocess env instead of the plaintext credential and
substitutes it immediately before egress, only for hosts bound to that secret. Present in our
vendored schema (`openclaw_config.schema.json:815`), so it would be accepted.
**Two caveats to state out loud before anyone calls this a security control:** it works by
putting `HTTPS_PROXY` in the exec env, and with `exec.mode: "full"` the agent can unset it;
and it does **not** cover our `env.vars`, which `provisioning_landing.build_provisioned_env`
re-asserts as plaintext at every provision. It covers the shared secret store only.
*Non-gap worth recording:* the **private credential request flow** (masked prompt, value never
enters chat or model context) is **already ours** —
`components/chat/QuestionCards.tsx:68-175` renders store-bound secret questions with the host
list and the replacement warning. No action.

### H9 · Capability-sweep DROP list — do not chase these
- **Cloud workers / Crabbox / placement — NOT-FOR-US, and it is not our infrastructure.**
  Read in full rather than from the changelog. `docs/gateway/cloud-workers.md` requires the
  `crabbox` binary ≥0.41.1 on the gateway host's PATH, cloud provider credentials for the OS
  user running the gateway, a **live registry-owned managed worktree** (plain directories are
  explicitly non-dispatchable), a public TLS endpoint the leased box dials back to, and a
  forwarded bootstrap-artifact route. Mapped onto us: each owner's container shells out to a
  CLI that leases EC2 against our account, per session. **Not a version of what we run — a
  second, worse copy pointed at the wrong blast radius.**
  The one adjacent piece, cloud lifecycle suspend/resume (#130242), is conceptually our trial
  scale-to-zero, but ours lives at the ECS layer (`connection_pool._grace_close` /
  `_maybe_stop_trial_container`) and theirs is inseparable from crabbox leases.
  **ALREADY-HAVE-BETTER for our shape.**
- **Device session hosting** — NOT-FOR-US on product grounds before technical: our owners want
  work to continue while their laptop is shut. The one sub-case with real value (driving the
  owner's logged-in browser) we already have via `gateway.nodes.browser.mode`.
- **The worktree wall — UNREACHABLE as a group.** We never set `worktree`; agents work in plain
  `workspace-{agent_id}/` on EFS. That makes `github_publish` ("publish the current
  **session-owned Git worktree**", `github-publish-tool.ts:23`), `projects.*` (built on
  `listRegistryWorktrees`, `server-methods/projects.ts:19-20`) and worktree-anchored permission
  modes all unreachable. Note the tools are still *exposed* to our models, so an agent can call
  `github_publish` and get an error — see H5.
- **Browser profile import / login sync — UNREACHABLE as shipped.** Throws
  *"system profile cookie access is only supported on macOS in this release"*
  (`system-profiles.ts:90-97`) and refuses to run through a proxy
  (`browser-tool.lifecycle.ts:158-161`). Our browser host is a Linux container.
  **But the owner need is real** — "let my agent use my logged-in accounts" — and we have a
  cheaper path upstream doesn't: our fork patch already snapshots cookies + per-origin
  localStorage to one JSON on EFS (`browser.sessionState`, `config.py:2365`). A Tauri-side
  exporter writing that file is a much smaller build. **Worth a separate design.**
- **Automations / `/loop` / workboards / goals — NOT-FOR-US**, they duplicate Paperclip.
  `/loop` is pure prompt sugar (rewrites chat text into an `automations` work-order,
  `commands-loop.ts:56-70`). Verified our fleet-wide deny still bites at 8.1 despite the
  `cron` → `automations` rename: `cron` is a permanent alias in the policy matcher
  (`tool-policy-shared.ts:19-24`). No bug.
  **One idea worth stealing into Paperclip:** `pacing: {min, max}` — self-paced automations
  where the agent picks its own next check interval via `automations action:"next_check"` and
  backs off when quiet. Our routines are fixed-cron. A Paperclip backlog line, nothing more.
- **Memory `forget` / provenance — ADOPT-LATER, low.** Real, and reachable from the agent via
  exec, but there is no owner UI and nobody has asked. The admission-policy knob
  (`memoryPolicy.excludeSessions`) matches only on hook source, channel id or chat type —
  exact, case-sensitive, no globs — so it **cannot** target our Paperclip routine sessions,
  which is the noise we'd actually want out of the nightly dream. The SQLite-backend concern is
  moot: 8.1 retired the qmd backend and moved search config to root `memory.search`, which our
  config already handles (`config.py:1954-1975`).
- **Checked and clean:** gateway control-plane rate limiting is 30/60s per (client, method) and
  applies only to `controlPlaneWrite` methods (`control-plane-rate-limit.ts:7`) — `chat.send`
  is not among them. Session-reset default flipped to "never reset when unconfigured"; we set
  it explicitly, unaffected. `openclaw.changes.list` predates 8.1. Backups —
  **ALREADY-HAVE-BETTER**, we replicate with litestream.

---

## DROP — investigated, rejected. Do not re-litigate without new facts.

- **`sessions.groups.*`** — gateway-global catalog, no per-client scoping
  (`sessions-groups.ts:39-49`); leaks group names across org members on a shared
  container. Use per-session `category` (A7).
- **Session dashboards / `boardFace:"dashboard"` / widget pinning** — rendering lives
  in the Control UI canvas host, which we disable. Consuming it means reimplementing
  the widget renderer.
- **`involvingMe` / `ownerId` / `creatorId` / `ownerFirst` / `includePeople`** —
  resolve against one shared backend operator identity (`sessions-read.ts:296-298`),
  not the end user. Cannot replace our post-filter. *(Revisit only if the identity
  lane finds per-member operator profiles are viable.)*
- **`sessions.dispatch` / `move` / `reclaim`** — require managed worktrees and
  cloud-worker or paired-device placement (`sessions-dispatch.ts:86-108`). We run
  neither. *(Capability-sweep lane is re-examining the strategic question.)*
- **`sessions.companion.ask`** — works, but costs a utility-model call per question,
  is rate-limited per connection, and is an unguarded cross-member transcript read
  until SEC-1 lands.
- **`sessions.count`** — not a real gateway method; appears only in a prewarm test
  fixture.

---

## Lanes still outstanding

Eight parallel audits against the shipped tag. One landed; appending the rest as they do.

| lane | scope | status |
|---|---|---|
| A | session surface | **landed** — SEC-1, A2–A11 above |
| B | gateway RPC + tool surface | **landed** — B1–B9 above. Protocol unchanged at 4; our `minProtocol: 4` is correct. |
| C | `openclaw.json` config drift | **landed** — C1–C8 above |
| D | chat event/stream transform vs what 8.1 emits | **landed** — D1–D11 above |
| E | skills, plugins, image bakes | **landed** — E1–E11 above |
| F | UI + interaction design | **landed** — UI0–UI14 + keep/retired/no lists above |
| G | multiplayer / collaboration / identity | **landed** — G-F1…G-F8 above; SEC-1 and A11 revised in place |
| H | capability sweep | **landed** — H1–H9 above. **All eight lanes complete.** |
