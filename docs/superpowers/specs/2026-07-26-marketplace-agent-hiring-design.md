# Marketplace agent hiring — making the team growable from inside the container

Status: design, approved to write 2026-07-26.

## Problem

A first prod free-tier run (`user_3H1NQ7qf9BGed0PA0eE1WRWHH5i`, company
`6ba8f56b`, 2026-07-26) produced this:

| Time (UTC) | Event |
|---|---|
| 02:10:45–02:11:15 | Friday files ADM-1/2/3 from the onboarding kickoff — all `backlog`, all `assignee_agent_id = NULL` |
| 02:26:43 | ADM-4 "Scaffold Jarvis — engineering agent for isol8.co" → assigned to Atlas |
| 02:28:03 | ADM-5, a near-duplicate of ADM-4, 80s later → also assigned to Atlas |
| 02:30:50/52 | both Atlas runs fail: `FailoverError: ⚠️ Throttling error: isol8: daily free limit reached` (`error_code=provider_quota`) |

`billing_accounts.trial_daily_spend_microcents["2026-07-26"] = 2758514` —
**$2.76 of the $3.00 cap**, essentially all of it spent by the onboarding
research run before the owner typed anything.

Two distinct defects:

1. **Friday reached for Atlas and never mentioned the marketplace.** Prod has
   8 published listings, all free — including `pulse`
   (`marketing, intelligence, content, competitive, research`), a direct fit
   for the content work in ADM-2. She had no way to know they exist.
2. **The path she did take was the expensive one.** A marketplace hire costs
   $0 inference (tarball extract + config merge). Atlas is a full
   agent-building run. On a $3/day cap that ordering is backwards.

### Why she never mentioned it — root cause

Not a model lapse. Three structural causes, each verified:

- **No agent-facing read path.** `grep -rn marketplace apps/infra/openclaw/skills/`
  returns zero API references. The `isol8` skill's marketplace section is
  narrative only. Contrast its own Integrations section: *"Never recite the
  connectable services from memory — they change. Read the current catalog
  live: `GET $ISOL8_API_URL/api/v1/platform-info`."* The marketplace never got
  that treatment, despite
  `GET /api/v1/marketplace/listings/search` already being public and
  unauthenticated (verified: HTTP 200 with no credentials) and `ISOL8_API_URL`
  already being in the container env.
- **`AGENTS.md` orders the options Atlas-first** — *"Recommend **Atlas** … Or
  recommend the marketplace for a pre-built one."* Atlas is an action Friday
  can take; the marketplace is a URL she hands back. Models take the action.
- **The search response is unusable by an agent anyway** — ~50KB for 8
  listings, dominated by `manifest_json` (file counts, safety flags,
  requirements). ~15k tokens Friday cannot afford under the cap.

Ruled out by inspection, so they are not causes: the `isol8` skill IS baked into
the running image (prod task def runs `openclaw-extended:2026.7.2-isol8.2-5fecaee`);
no `skills.allowBundled` allowlist is configured, so bundled skills are not
filtered; `AGENTS.md` is 11,075 chars against a 20,000-char per-file bootstrap
budget, so it is not truncated.

## Goals

- Friday can discover what is actually in the marketplace, by name, cheaply.
- Hiring is one click, in context — no "go to isol8.co/marketplace".
- Hiring works when Friday is running headless in a Paperclip heartbeat.
- **Hiring is strictly approval-based.** The roster never changes without an
  explicit human decision, on every surface.

## Non-goals

- **Agent-autonomous hiring.** Explicitly rejected — see Decisions.
- **Paid-listing automation.** Money always moves through Stripe checkout on
  the owner's own session.
- **Fixing onboarding's cost profile.** The $2.76-of-$3.00 burn is the deeper
  bug and lands in its own spec; a hire card in a container that cannot fund
  the agent it hired is still an improvement, just an incomplete one.

## Decisions

### D1 — The plugin is read-only. Nothing agent-side mutates the roster.

`tools.exec.mode = "full"` fleet-wide (`core/containers/config.py:355`).
Approvals are disabled deliberately:

> *"agents run autonomously via the Paperclip openclaw_gateway adapter … with
> NO operator connected to answer an approval. The previous
> `security="allowlist" / ask="on-miss"` policy made every non-allowlisted exec
> in an autonomous run hang on an approval that never arrives, until the 120s
> gateway timeout — Paperclip then re-dispatched, looping forever."*

So a write tool gated on an OpenClaw approval would hang and re-dispatch inside
a Paperclip run — reproducing a loop already fixed once. Making the plugin
read-only means "strictly approval-based" is **structural, not behavioural**:
Friday has no capability to hire, so she cannot do it wrong.

This also removes the need for the sibling write endpoint an earlier draft
proposed alongside `POST /agent-builder/u/{owner_id}/register-built-agent`.

### D2 — Approval reuses Paperclip's existing `hire_agent` type.

`server/src/services/approvals.ts` (fork, branch `isol8/adapter-run-allowance`)
already has `type: "hire_agent"`. On approve it activates a pre-created
`pending_approval` agent (`payload.agentId`) or creates one from payload
fields, applies a budget policy when `budgetMonthlyCents > 0`, and calls
`notifyHireApproved`. On reject it terminates the pre-created agent.
`requestRevision` gives a third outcome (`revision_requested`).

Isol8 already proxies the whole surface (`routers/teams/approvals.py`) and
renders it (`InboxPage` → `NeedsAttentionBand` → `ApprovalRow`,
`useApprovalMutations`). This is the "assign it back to the user" affordance,
and it is already built.

`notifyHireApproved` invokes the adapter's `onHireApproved` hook, but **no
adapter implements it** — across the whole fork, `onHireApproved` appears only
in `services/hire-hook.ts` (the caller), its test, and the
`packages/adapter-utils/src/types.ts` type declaration. `openclawGatewayAdapter`
(`server/src/adapters/registry.ts:387`) is no exception, so the hook returns
early at `if (!onHireApproved) return;`. **No auto-wake, therefore no race**
between roster-entry creation and workspace deployment.

⚠️ This is an *unimplemented interface*, not a deliberate opt-out. If a future
upstream merge implements `onHireApproved` for `openclaw_gateway`, Paperclip
would wake the hired agent immediately on approve — before `approvals.py` has
deployed its workspace — and it would wake into a container where it does not
yet exist in `openclaw.json`. Re-check this on any Paperclip fork rebase; if it
lands, the fix is to deploy the artifact before calling Paperclip's approve
rather than after.

### D3 — A plugin, not MCP, not a skill script.

- `defineToolPlugin` is purpose-built for tool-only plugins and gives a typed
  `outputSchema`, which Code Mode / Tool Search compile into a bounded output
  hint — real token savings under the cap.
- The typed chain is verified end to end: plugin returns JSON → OpenClaw keeps
  it in `AgentToolResult.details` → `embedded-agent-subscribe.handlers.tools.ts:1390`
  sets `eventResult = sanitizedResult` for non-exec tools → emitted as
  `stream:"tool", phase:"result"` → `connection_pool._transform_agent_event`
  forwards it as `tool_end.result`. **No gateway or backend event changes.**
- MCP was rejected: this codebase deliberately removed Paperclip-over-MCP
  ("*the MCP env is container-wide + shared-frozen, so every agent that called
  it acted as Friday — wrong attribution*").
- A curl skill script was rejected for the card: its output arrives as exec
  stdout, and parsing that for structure is the known-bad
  no-structured-marker trap.

Rebuild-cycle cost is mitigated by keeping the plugin dumb — it is a typed
shell over HTTP; ranking, projection, and policy live in the backend and
iterate without an image rebuild.

## Architecture

```
┌─ CONTAINER ───────────────────────────────┐
│  plugin: isol8-marketplace  (READ-ONLY)   │  baked root-owned at
│    marketplace_search(query)              │  /app/dist/extensions/isol8-marketplace
│    recommend_agents(slugs, reason)        │  enabled via plugins.entries in config.py
└───────────────┬───────────────────────────┘
                │ HTTPS, Bearer OPENCLAW_HOOK_TOKEN
                ▼
┌─ BACKEND ─────────────────────────────────┐
│  GET  /marketplace/listings/search        │  + ?view=compact  (NEW)
│  POST /listings/{id}/deploy               │  UNCHANGED (get_current_user)
│  POST /teams/approvals/{id}/approve       │  + post-approve artifact deploy (NEW)
└───────────────▲───────────────────────────┘
                │ user's own Clerk session
┌─ FRONTEND ────┴───────────────────────────┐
│  chat:  tool_end.result → HireCard        │
│  teams: ApprovalRow (already exists)      │
└───────────────────────────────────────────┘
```

**Trust boundary:** every mutation runs on a human click carrying that human's
Clerk session. There is no privileged write path added by this design.

## Components

### C1 — Plugin `isol8-marketplace`

Source in-repo at `apps/infra/openclaw/plugins/isol8-marketplace/`, built with
`openclaw plugins build`, `COPY`'d into `/app/dist/extensions/isol8-marketplace`
root-owned (same pattern as the baked `@openclaw/*` plugins — the loader rejects
plugin dirs not owned by uid 0). Enabled via `plugins.entries` in
`core/containers/config.py`.

`marketplace_search(query: string)` → compact listing array. Ordinary collapsed
tool pill; no card.

`recommend_agents(slugs: string[], reason: string, issue_id?: string)` →
resolves slugs server-side and returns name/summary/tags/price per listing.
Friday cannot render a card for a listing that does not exist, and the card
cannot drift from the catalog. `outputSchema` declared with
`additionalProperties: false`.

`issue_id` is what disambiguates the two surfaces, and it is explicit rather
than inferred: Friday already receives `PAPERCLIP_TASK_ID` in her wake text on
any Paperclip-driven run (and has none in webchat). Passing it means "I am
working a task, no one is watching" → the backend creates a `hire_agent`
approval linked to that issue. Omitting it means "chat" → pure read, card
renders. The backend cannot infer run context from the HTTP call alone, so
this must be an argument, not a heuristic.

Friday **also names the agent in prose** alongside the tool call, so the
recommendation stays legible on Telegram/email where no card renders.

### C2 — Compact search projection

`GET /marketplace/listings/search?view=compact` returns
`{slug, name, summary, tags, price_cents}` only — no `manifest_json`,
`s3_prefix`, `manifest_sha256`, or `safety_flags`. Target: under 2KB for the
current 8 listings, against ~50KB today. Default (no `view`) is unchanged so
the existing frontend callers are untouched.

### C3 — Hire card (chat)

Renders in `MessageList` when `tool_end.tool === "recommend_agents"`, from
`result.details`. Per listing: name, one-line summary, tags, price, and a
**Deploy to my team** button calling the existing
`POST /marketplace/listings/{id}/deploy` with the user's session. Free
listings deploy directly; paid ones route to Stripe checkout.

### C4 — Teams approval path

When Friday calls `recommend_agents` **with `issue_id`**, the backend creates a
Paperclip `hire_agent` approval carrying the listing slug in
`payload.metadata`, and Friday marks the issue blocked and exits the heartbeat
— the behaviour `AGENTS.md` already mandates. No busy-poll, no hang, no
re-dispatch.

**The approval payload must carry `adapterType: "openclaw_gateway"` and the
matching `adapterConfig.url` (`wss://ws-<env>.isol8.co`).** Paperclip's approve
path defaults it: `adapterType: String(payload.adapterType ?? "process")`. An
agent created with `"process"` is unreachable on Isol8 and would fail silently
at first wake, long after the approval looked successful. This is separate from
the BFF's existing `extra="forbid"` guard, which blocks `adapterType` on the
*approve* body — here we are authoring the *create* payload and must set it
deliberately. `payload.name` and `payload.role` come from the listing;
`payload.agentId` is deliberately omitted so Paperclip creates the agent on
approve rather than activating a pre-created one (nothing should exist in the
roster before the human says yes).

The approval appears in the Teams Inbox "Needs Attention" band. On approve,
`routers/teams/approvals.py::approve` — already Isol8 code on the user's
authenticated request — proxies to Paperclip as today, then, when the approval
is a marketplace hire, calls `catalog.deploy_from_artifact(new_agent_id=<id
Paperclip returned>, …)` with the listing's tarball + slice from S3.

Known asymmetry: the chat path mints via `create_paperclip_for_marketplace`,
the Teams path mints via Paperclip's own `agentsSvc.create`. Both converge on
`deploy_from_artifact`; the artifact-fetch-and-deploy half is extracted into
one shared helper so the two paths cannot drift.

### C5 — Copy (the part that actually fixes the reported bug)

**`isol8` SKILL.md** — marketplace section gets the sentence the Integrations
section already has: never recite listings from memory, read the catalog live
via `marketplace_search`. Plus the cost fact: a marketplace hire is $0
inference, Atlas is a full agent-building run.

**`friday_identity._AGENTS_TEMPLATE`** — step 3 stops leading with Atlas. New
rule: *check the marketplace first; route to Atlas only when nothing fits.*
Both options still get presented to the owner, per the existing Confirm step.

C5 ships independently of C1–C4 and is the highest value-per-line change here.

## Data flow

**Chat.** Friday hits work nobody on the roster covers → `marketplace_search`
→ `recommend_agents(["pulse"], …)` → `tool_end.result` → hire card → user
clicks Deploy → `POST /listings/{id}/deploy` → mint + `deploy_from_artifact` →
agent appears in roster and in `openclaw.json`.

**Teams heartbeat.** Same up to `recommend_agents(…, issue_id=$PAPERCLIP_TASK_ID)`
→ backend creates `hire_agent` approval → Friday comments, marks the issue blocked, exits →
approval lands in Inbox → owner approves → Paperclip creates the roster entry
→ `approvals.py` deploys the artifact → next heartbeat sees the new agent in
the roster.

## Error handling

- **Listing unpublished/retired between recommend and click** — deploy route
  already 404s on non-`published`; the card surfaces it and offers a re-search.
- **Deploy fails after Paperclip mint** — existing rollback envelope
  (`delete_paperclip_for_marketplace`) already covers this in both
  `deploy_listing` and `register_built_agent`; the shared helper inherits it.
- **Approval approved but artifact deploy fails** — Paperclip has already
  created the roster entry, so the owner sees an agent with no workspace. The
  approve handler rolls the Paperclip agent back and returns a retryable error
  rather than leaving a half-hired agent.
- **Trial cap reached** — `marketplace_search` is a backend HTTP call and costs
  no inference, so discovery keeps working under the cap. Deploying keeps
  working too; only *running* the hired agent is gated, and it fails with the
  existing daily-limit message.
- **Hallucinated slugs** — impossible by construction: `recommend_agents`
  resolves server-side and drops unknown slugs.

## Testing

- Plugin: `openclaw plugins validate` + vitest on tool metadata and
  `outputSchema` conformance.
- Backend: compact projection excludes every heavy field; approve-path deploy
  fires only for marketplace-hire approvals; rollback on deploy failure;
  the created approval payload carries `adapterType: "openclaw_gateway"`
  (regression guard for the `?? "process"` default).
- Frontend: `HireCard` renders from a `tool_end` fixture; unknown tool names
  fall through to the ordinary pill.
- Manual, on dev, before any prod tag flip: confirm a trial-tier container
  (`is_bedrock_trial`, where `toolSearch` is deliberately off) loads the plugin
  and both tools appear in Friday's toolset.

## Follow-ups (explicitly out of scope)

1. **Onboarding cost profile** — $2.76 of $3.00 spent before first user input.
2. **Duplicate-issue guard** — ADM-4/ADM-5 were near-identical, created 80s
   apart, both assigned to Atlas. Worth checking whether
   `issue_create_idempotency_keys` is exercised on Friday's create path.
3. **Agent-completed hiring via Paperclip approval wake** —
   `PAPERCLIP_WAKE_REASON=approval` could let Friday finish the hire herself
   after the owner approves, instead of the backend doing it. More faithful to
   "the agent hires," but needs a privileged endpoint plus backend verification
   of Paperclip approval state. Revisit if headless hiring proves common.
4. **No engineering agent exists in the marketplace.** For ADM-4's actual need,
   Atlas remained the only correct path — Friday's judgment was right, it just
   could not execute. A seeded engineering listing would close the most common
   gap.
