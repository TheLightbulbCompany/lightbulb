# BOOTSTRAP.md

One-time setup for Atlas. Runs on first activation, then deletes itself.

Everything here is **gateway-free**: durable state lives under `atlas/*` keys via
native `state.get` / `state.set`, real programs run as bare `node`, and the only
external surface Atlas reaches at build time is the backend bridge (see Step 1).

## Step 1: Validate Prerequisites

Required environment variable:
- `OPENCLAW_HOOK_TOKEN` — the per-container webhook token. It authenticates the
  build-time CREATE call to the backend bridge as `Authorization: Bearer
  $OPENCLAW_HOOK_TOKEN`.

The bridge endpoint `build.lobster` curls to register a built agent:
- `POST /api/v1/agent-builder/u/{owner_id}/register-built-agent` — snapshots the
  staging workspace, mints a Paperclip agent, and deploys it live. The concrete
  per-owner URL is substituted into `build.lobster` at stage time (the vendored
  file ships a literal `{{ISOL8_BRIDGE_URL}}` placeholder). Confirm the
  placeholder was substituted; if it still reads `{{ISOL8_BRIDGE_URL}}`, refuse to
  proceed — the build pipeline cannot register agents.

LLM provider/model selection is handled by the isol8 platform; the agent loop
drains queued LLM intents and lets the platform route. The agent does not read
user-account or billing config.

## Step 2: Initialize Default State

Seed the durable `atlas/*` keys (native `state.set`). Each per-build key holds a
map keyed by `build_id`; the queues are arrays.

```
state.set atlas/intakes                   {}
state.set atlas/research-briefs           {}
state.set atlas/requirement-matrix        {}
state.set atlas/decisions                 {}
state.set atlas/vet-progress              {}
state.set atlas/recaps                    {}
state.set atlas/cost-estimates            {}
state.set atlas/notifications             []
state.set atlas/audit-log                 []
state.set atlas/failure-cases             {"version": 1, "cases": []}
state.set atlas/cookbook-docs             {}
state.set atlas/cookbook-agent-snapshots  {"count": 0, "agents": []}
state.set atlas/cookbook-refresh-state    {"last_refreshed": null, "indexed_urls": []}
state.set atlas/citation-tracking         {"citations": {}}
```

## Step 3: Verify Notification Channels

The channels named in USER.md (`approval_channel`, `vet_channel`,
`status_channel`) must exist. The pipelines route every human-facing surface
(vet rows, sign-off prompts, build outcomes, stale-citation warnings) as intents
on `atlas/notifications`; the agent loop drains them and posts to these channels.
If any channel is missing, surface to the user and refuse to proceed.

## Step 4: Register Cron Jobs

The operational crons map 1:1 to pipelines / tools. Register them as cron rows on
the persisted cron-jobs shape (direct write to `~/.openclaw/cron/jobs.json`, the
same gateway-free mechanism `atlas-trigger-registrar.js` uses for built agents —
no `openclaw cron add` CLI, which is gateway-bound):

```
cookbook-refresh        "0 4 * * *"      cookbook-refresh.lobster              # daily doc + agent-snapshot refresh
failure-case-refresh    "0 3 * * 1,4"    node atlas-failure-case-router.js --refresh   # Mon + Thu
capability-evolve       "0 6 * * 0"      node skills/capability-evolver/index.js        # weekly Sun: tune v1-default thresholds
audit-rollup            "0 23 * * *"     node atlas-cost-counter.js --rollup-day         # nightly audit-log rollup
citation-verification   "0 4 1 */3 *"    citation-verification.lobster          # quarterly citation re-verification
```

failure-case-refresh runs Mon AND Thu (twice-weekly).
citation-verification runs quarterly (every 3 months on the 1st at 04:00).

## Step 5: Configure Webhooks

Three endpoints under `/hooks`:
- `POST /hooks/atlas-build-start` → triggers `intake-and-research.lobster`
- `POST /hooks/atlas-vet-decision` → records a per-requirement vet decision (the
  agent loop normalizes it and re-invokes `vet-walk.lobster` for the next row)
- `POST /hooks/atlas-build-confirm` → triggers `build.lobster` after S9
  architecture sign-off + S9.5 determinism audit clear

There is no publish webhook — publishing is the owner's action in the seller
wizard UI, not an Atlas pipeline.

## Step 6: Run Security Checks

Run the installed security skills (local skills, run directly — not via a gateway
tool proxy):

```
skill-vetter --action audit --target all_installed_skills
sona-security-audit --action enable
```

skill-vetter runs against the full installed skill set; refuse to proceed if any
vetter check fails. `sona-security-audit` provides runtime monitoring thereafter.
The build-time hard confirms (S9 sign-off, S10 file-write) are native `approval:`
gates in the pipelines — `agentgate` remains installed as the underlying approval
skill, but the pipelines surface the gates natively, not via a gateway tool call.

## Step 7: Run Activation Check

```
node atlas-activation-check.js
```

Validates: `OPENCLAW_HOOK_TOKEN` present, bridge URL substituted (no leftover
`{{ISOL8_BRIDGE_URL}}`), `atlas/*` state defaults initialized, cron jobs
registered, notification channels verified, security stack installed, and
openclaw.json `tools.deny` includes the full ClawHavoc list.

## Step 8: Announce Readiness and Delete This File

Post to the user's `status_channel`:

> "Atlas is ready. Build_id pinned: `atlas-2026-04-28`. To start a build: POST
> /hooks/atlas-build-start with `{user_request: 'your description'}`. When a build
> finishes I register the agent live and hand you its `agent_id` — you publish it
> to the marketplace via the seller wizard if you choose to. See AGENTS.md for the
> methodology and SOUL.md for how I'll talk to you."

Then: `rm BOOTSTRAP.md`. Subsequent activations skip these steps.
