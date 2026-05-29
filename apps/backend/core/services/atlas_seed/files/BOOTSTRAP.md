# BOOTSTRAP.md

One-time setup for Atlas. Runs on first activation, then deletes itself.

## Step 1: Validate Prerequisites

Required environment variables:
- `OPENCLAW_HOOK_TOKEN` — webhook authentication

LLM provider/model selection is handled by the isol8 platform; Atlas calls `llm-task` and lets the platform route. The agent does not read user-account or billing config.

## Step 2: Initialize Default fast-io State

```
fast-io write atlas-config/identity-locks       {}
fast-io write atlas-config/cookbook-refresh-state  {"last_refreshed": null, "last_failure_case_refresh": null, "last_citation_verification": null}
fast-io write atlas-cookbook/openclaw-docs      {}
fast-io write atlas-cookbook/agent-snapshots    {}
fast-io write atlas-cookbook/failure-cases.json {"version": 1, "cases": []}
fast-io write atlas-cookbook/user-resolutions   {}
fast-io write atlas-builds                      {}
fast-io write atlas-pending-submits             {}
```

## Step 3: Verify Slack Channels

The Slack channels named in USER.md (`approval_channel`, `vet_channel`, `build_status_channel`) must exist. If any is missing, surface to user and refuse to proceed.

## Step 4: Register Cron Jobs

```
openclaw cron add cookbook-refresh         "0 4 * * *"        "node atlas-cookbook-reader.js --refresh"      --session isolated --light-context
openclaw cron add failure-case-refresh     "0 3 * * 1,4"      "node atlas-failure-case-router.js --refresh"  --session isolated --light-context
openclaw cron add capability-evolve        "0 6 * * 0"        "openclaw.invoke --tool capability-evolver --action analyze --args-json '{\"window_days\": 7}'"  --session isolated --light-context
openclaw cron add audit-rollup             "0 23 * * *"       "node atlas-cost-counter.js --rollup-day"      --session isolated --light-context
openclaw cron add pending-submits-prompt   "0 9 * * 1"        "node atlas-marketplace-submit.js --prompt-pending"  --session isolated --light-context
openclaw cron add citation-verification    "0 4 1 */3 *"      "node atlas-citation-verifier.js --quarterly" --session isolated --light-context
```

failure-case-refresh runs Mon AND Thu (twice-weekly).
citation-verification runs quarterly (every 3 months on the 1st at 04:00).

## Step 5: Configure Webhooks

Four endpoints under `/hooks`:
- `POST /hooks/atlas-build-start` → triggers `intake-and-research.lobster`
- `POST /hooks/atlas-vet-decision` → records per-requirement vet decision (drives `vet-walk.lobster` loop)
- `POST /hooks/atlas-build-confirm` → triggers `build.lobster` after S9 architecture sign-off
- `POST /hooks/atlas-publish-confirm` → triggers `publish.lobster` after S11 marketplace-submit confirmation

## Step 6: Run Security Checks

```
openclaw.invoke --tool skill-vetter --action audit --args-json '{"target": "all_installed_skills"}'
openclaw.invoke --tool sona-security-audit --action enable
openclaw.invoke --tool agentgate --action verify-installed
```

skill-vetter runs against agentgate first. Refuse to proceed if any vetter check fails.

## Step 7: Run Activation Check

```
node atlas-activation-check.js
```

Validates: env vars present, fast-io defaults initialized, cron jobs registered, Slack channels verified, security stack installed, openclaw.json `tools.deny` includes the full ClawHavoc list.

## Step 8: Announce Readiness and Delete This File

Post to user's `build_status_channel`:

> "Atlas is ready. Build_id pinned: `atlas-2026-04-28`. To start a build: POST /hooks/atlas-build-start with `{user_request: 'your description'}`. See AGENTS.md for the methodology and SOUL.md for how I'll talk to you. Build Session Learnings appendix in AGENTS.md documents the methodology working as designed during prior builds."

Then: `rm BOOTSTRAP.md`. Subsequent activations skip these steps.
