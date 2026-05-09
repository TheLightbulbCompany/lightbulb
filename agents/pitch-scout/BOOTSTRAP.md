# Pitch + Scout — First Run Bootstrap

This file runs once on first activation. Complete all steps, then delete this file.

This is a combined deployment: Scout (sourcing) and Pitch (outreach) share one workspace, one CRM connection, and one set of audit / competitor / enrichment-cache keys. Outbound email platforms, CRMs, and optional integrations are click-to-connect toggles in the Isol8 settings UI — neither stack interrogates the user during bootstrap.

## Step 1: Validate Prerequisites

Required environment:
- `PERPLEXITY_API_KEY`
- `APOLLO_API_KEY`
- `OPENCLAW_HOOK_TOKEN`

That's it. No outbound platform or CRM key is required at bootstrap — the user connects those from the settings UI before asking the agent to send outreach or sync CRM data.

If required keys are missing, message the user via Slack explaining which are needed. Do not proceed until required keys are configured.

## Step 2: Initialize Fast-io Keys

Sourcing (Scout) keys:
- `scout-config/volume-limit` → `{"daily_limit": 50}`
- `scout-config/signal-weights` → `{}` (populated by capability-evolver after first week)
- `scout-config/exclusivity-weights` → default exclusivity map from scout-icp-scorer.js
- `scout-config/connected-crm` → `null` (mirrors `pitch-config/connected-crm`)
- `scout-config/connected-enterprise` → `{}` (user connects optional services via settings)
- `scout-config/pitch-installed` → `true` (combined deployment — leads route to Pitch's queue)
- `scout-state/today-deposits` → `{"count": 0, "date": "{{today}}"}`

Outreach (Pitch) keys:
- `pitch-config/icp` → empty object (rep populates before outreach begins)
- `pitch-config/launch-state` → `{"phase": "test_cohort", "accounts_processed": 0, "cohort_reviewed": false}`
- `pitch-config/activation` → will be populated by activation check
- `pitch-config/connected-outbound` → `null` (user connects via settings)
- `pitch-config/connected-crm` → `null` (user connects via settings)
- `timing-weights/` → default timing rules

Shared keys:
- `competitor-list` → `[]` (rep populates with competitor names)

## Step 3: Create Slack Channels

Sourcing:
- `#scout-alerts` — urgent leads, source health, match rate warnings
- `#scout-digest` — daily completion summary, weekly intelligence report
- `#scout-leads` — optional, dormant in this combined config (kept for direct-visibility briefs)

Outreach:
- `#pitch-approvals` — approval gate delivery
- `#pitch-signals` — signal notifications
- `#pitch-pipeline` — pipeline health and MEDDIC alerts
- `#pitch-quarantine` — low-confidence prospect alerts

## Step 4: Set Up Cron Jobs

```
openclaw cron add --name "scout-daily-source" --cron "0 6 * * *" --tz "America/New_York" --session isolated --message "Run daily-source pipeline" --thinking low --light-context

openclaw cron add --name "scout-signal-monitor" --cron "0 */4 * * *" --tz "America/New_York" --session isolated --message "Run signal-monitor pipeline" --thinking low --light-context

openclaw cron add --name "scout-weekly-report" --cron "0 8 * * 1" --tz "America/New_York" --session isolated --message "Run weekly-report pipeline" --thinking low --light-context

openclaw cron add --name "scout-source-health" --cron "0 9 * * *" --tz "America/New_York" --session isolated --message "Run source health check" --thinking low --light-context

openclaw cron add --name "pitch-signal-sweep" --cron "0 */4 * * *" --tz "America/New_York" --session isolated --message "Run signal-sweep pipeline" --thinking low --light-context

openclaw cron add --name "pitch-sequence-execute" --cron "0 * * * *" --session isolated --message "Run sequence-execute pipeline" --thinking low --light-context

openclaw cron add --name "pitch-meddic-scan" --cron "0 8 * * 1-5" --tz "America/New_York" --session isolated --message "Run meddic-scan pipeline" --thinking low --light-context

openclaw cron add --name "pitch-bounce-check" --cron "0 9 * * 1" --tz "America/New_York" --session isolated --message "Run bounce rate health check" --thinking low --light-context

openclaw cron add --name "capability-evolve" --cron "0 6 * * 0" --tz "America/New_York" --session isolated --message "Run capability-evolver: analyze draft approval patterns, signal dismissal patterns, engagement timing patterns, signal weights, and score thresholds. Update voice model, signal weights, and timing weights in fast-io." --thinking low --light-context
```

Note: `scout-signal-monitor` and `pitch-signal-sweep` both run every 4h. They monitor different things (Scout: configured signal sources for ICP-confirmed briefs across all verticals; Pitch: signals tied to active sequences and prospect briefs already in `briefs/active/`). Keep both — they're not redundant.

## Step 5: Configure Webhooks

Wire external webhooks into OpenClaw's webhook endpoint.

**Outbound platform reply webhook** (Instantly / SmartLead / Mailshake / Lemlist) → `POST /hooks/agent`
```json
{"message": "Reply received from {{prospect_domain}}. Run reply-handler pipeline.", "name": "pitch-reply"}
```

**Outbound platform unsubscribe webhook** → `POST /hooks/agent`
```json
{"message": "Unsubscribe from {{prospect_domain}}. Run opt-out-handler pipeline.", "name": "pitch-optout"}
```

**CRM deal stage change webhook** (HubSpot / Salesforce / Attio / Pipedrive) → `POST /hooks/agent`
```json
{"message": "Deal stage changed for {{prospect_domain}} from {{old_stage}} to {{new_stage}}. Run meddic-stage-check pipeline AND log outcome for Scout conversion tracking.", "name": "pitch-stage-change"}
```

**CRM contact status change webhook** → `POST /hooks/agent`
```json
{"message": "Contact status changed for {{prospect_domain}} to {{new_status}}. Check for active sequences to interrupt.", "name": "pitch-status-change"}
```

**Visitor identification webhook** (Leadfeeder / Clearbit Reveal) → `POST /hooks/agent`
```json
{"message": "Website visitor detected: {{company_domain}} visited {{page}}. Run visitor-alert pipeline.", "name": "scout-visitor"}
```

The CRM stage-change webhook is consumed by both stacks — it's a single subscription that triggers Pitch's MEDDIC stage check AND Scout's outcome logging.

## Step 6: Run Security Checks

- Run skill-vetter against every installed skill before production. Especially `last30days` — flagged for prompt-injection pattern.
- Verify all skills have VirusTotal clearance.
- Confirm no skills under 30 days old on ClawHub are installed.
- Confirm none of the deny-listed skills are present: `salesforce-api`, `pipedrive-api`, `attio-api`, `linkedin-automation`, any "send LinkedIn at volume" skill.
- Enable sona-security-audit for runtime monitoring.

## Step 7: Run Activation Check and Announce Readiness

1. Run the activation check to verify all prerequisites pass (Pitch side).
2. Post a single ready message to `#pitch-approvals` (the rep's primary channel):

   "I'm live. As Scout, I monitor signals across your configured sources, enrich and score leads against your ICP, and deposit ready dossiers into the outreach queue. As Pitch, I take those dossiers, draft first touches for your approval, and run approved sequences with reply handling and MEDDIC tracking. When you're ready to start sourcing, tell me about your ideal customer and I'll set up signal monitors and database routing automatically. If you haven't connected your outbound platform or CRM yet, I'll let you know when it's time — no setup paperwork up front."

## Step 8: Delete This File

Delete BOOTSTRAP.md after all steps complete.
