# Pitch + Scout — Tool Usage Guide

## Lobster (Primary Orchestration)

All scheduled and event-driven operations run through Lobster pipelines. Do not manually execute pipeline steps through the agent loop — trigger the pipeline and let it run deterministically.

Sourcing pipelines (Scout):
- `daily-source.lobster` — main daily orchestrator (cron: daily 6am)
- `signal-monitor.lobster` — signal detection across all sources (cron: every 4h)
- `enrich-and-score.lobster` — waterfall enrichment + ICP scoring (triggered per lead)
- `deposit-lead.lobster` — dedup + volume limit + handoff to Pitch's queue (triggered per lead)
- `weekly-report.lobster` — Monday intelligence report (cron: Monday 8am)
- `visitor-alert.lobster` — high-urgency website visitor processing (webhook-triggered)

Outreach pipelines (Pitch):
- `signal-sweep.lobster` — buying signal detection (cron: every 4h)
- `prospect-enrich.lobster` — waterfall enrichment + ICP scoring on Scout-deposited or directly-detected prospects (triggered per prospect)
- `draft-and-approve.lobster` — first touch draft + approval gate (triggered after enrichment)
- `sequence-execute.lobster` — follow-up touches 2-5 (cron: hourly)
- `reply-handler.lobster` — prospect reply processing (webhook-triggered)
- `meddic-scan.lobster` — daily MEDDIC gap check (cron: daily 8am)
- `meddic-stage-check.lobster` — stage change MEDDIC check (webhook-triggered)
- `opt-out-handler.lobster` — opt-out processing (webhook-triggered)

When Scout finishes `deposit-lead.lobster`, the lead lands in `lead-queue/priority/` or `lead-queue/standard/` and Pitch's `prospect-enrich.lobster` picks it up from there. The two stacks coordinate via fast-io keys, not direct invocation.

## llm-task (Structured LLM Subtasks)

Use for any step that requires language understanding within a pipeline. Always specify:
- `thinking` level explicitly (off/low/medium — never high in pipelines)
- `schema` for structured JSON output
- Minimal `input` — only the data needed for that specific task

Thinking-level guide:
- `thinking: "off"` — classification (signal strength scoring, review sentiment classification)
- `thinking: "low"` — structured synthesis (research briefs, follow-up drafts, MEDDIC extraction, job posting interpretation — only the ~30% the keyword classifier misses, outreach angle generation — only the ~30% the deterministic map misses, weekly signal weight analysis)
- `thinking: "medium"` — first-touch drafts and reply drafts (Pitch-mode only)
- Never use `thinking: "high"` in any automated pipeline

Never use llm-task when a Node script can do the job. Deterministic scripts handle: vertical routing, ICP scoring, deduplication, volume limiting, enrichment caching, match rate monitoring, compliance checks, source health checks, job posting keyword classification (~70%), dossier assembly (~70%), signal recency weighting, and common signal-to-angle mappings.

## fast-io (Persistent Storage)

Shared key namespace across both stacks. Sourcing keys are prefixed `scout-`, outreach keys `pitch-`, and a few cross-cutting keys (`competitor-list`, `audit/`) are shared.

Scout keys:
- `scout-briefs/active/{{brief_id}}` — ICP briefs with vertical, roles, criteria, status, confirmation
- `scout-config/signal-weights` — capability-evolver-tuned weights
- `scout-config/exclusivity-weights` — exclusivity map
- `scout-config/volume-limit` — daily limit
- `scout-config/pitch-installed` — `true` in this combined deployment
- `scout-config/connected-crm` — CRM credentials (shared with Pitch)
- `scout-config/connected-enterprise/{service}` — optional enterprise integrations
- `signals/{{date}}/{{brief_id}}` — daily signal results
- `enrichment-cache/{{domain}}` — 30-day cached enrichment profiles
- `lead-queue/priority/{{domain}}` — priority tier leads (75+) → consumed by Pitch
- `lead-queue/standard/{{domain}}` — standard tier leads (50-74) → consumed by Pitch
- `lead-archive/{{domain}}` — below-threshold leads
- `lead-buffer/{{date}}/{{domain}}` — overflow leads for next day
- `scout-state/today-deposits` — daily deposit counter

Pitch keys:
- `pitch-config/icp` — ICP criteria
- `pitch-config/activation` — activation gate state
- `pitch-config/launch-state` — test cohort phase
- `pitch-config/connected-outbound` — connected outbound email platform config
- `pitch-config/connected-crm` — connected CRM platform config (mirrors Scout's; same record)
- `briefs/active/{{domain}}` — qualified prospect briefs (post-enrichment)
- `briefs/archived/{{domain}}` — sub-threshold prospects
- `briefs/quarantine/{{domain}}` — low-confidence prospects
- `sequences/active/{{domain}}` — active sequence state
- `opt-out/{{domain}}` — opt-out records
- `timing-weights/` — engagement timing optimization data

Shared keys:
- `competitor-list` — configured competitor names for content scanning + review-site monitoring
- `audit/{{timestamp}}/{{action}}/{{domain}}` — 12-month audit trail (both stacks write here)

## Perplexity API (Research + Signal Detection Engine)

Primary structured-web-search engine for both stacks. Used for buying signal detection (funding news, leadership changes, job postings, regulatory announcements), review site monitoring, PR wire monitoring, and recent news enrichment.

**Not a ClawHub skill.** Called via direct `curl` from lobster `exec --shell` steps. The Perplexity `sonar` model is designed for structured web search with citations: every response has a `choices[0].message.content` plus a `citations[]` array of source URLs. This is a deterministic API — same query, same structured response shape — so every call is a deterministic step, NOT an llm-task call. The deterministic / LLM split is preserved exactly.

Canonical call shape:
```
curl -sS https://api.perplexity.ai/chat/completions \
  -H "Authorization: Bearer $PERPLEXITY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"sonar","messages":[{"role":"user","content":"<query>"}]}'
```

In lobster pipelines, curl is piped into an inline `node -e` normalizer that extracts `content`, `citations`, `total`, and any other fields downstream steps expect. Auth credential `PERPLEXITY_API_KEY` is set at the process environment level (not in `openclaw.json`).

## Skills

### apollo — Primary enrichment, email verification, and funding signals
275M+ contacts with verified emails. First waterfall stop for technology and SaaS verticals only. Do NOT use as a fallback universal database for healthcare, legal, finance, or manufacturing — coverage is materially weaker there. The vertical router handles this automatically. Apollo also provides `verification_status` (replaces dedicated email verification) and recent funding data (supplements SEC EDGAR and Perplexity for funding signals).

### agent-browser — Portal navigation and deep web extraction
Headless browser for sites that block standard HTTP access and for deep content extraction. Used for: ThomasNet, GovWin, CoStar, Martindale-Hubbell, LinkedIn nonprofit searches, careers pages, press releases, blog posts, monitoring portals without APIs (state procurement, county property records), and any site requiring login or JS interaction. More expensive than Perplexity in time — only use when structured snippets aren't enough.

### gog — Google Workspace
- Gmail: **Reply sends only** — when a prospect has replied and the rep approves a response, send through gog so it comes from the rep's real inbox. Never used for cold outreach.
- Google Sheets: human-readable lead tracking, exportable reports.
- Google Drive: voice model storage and versioning, weekly report storage.

### slack — Approvals, alerts, and digests
Sourcing channels:
- `#scout-leads` — lead dossiers (when Pitch isn't installed; in this combined deployment leads route to Pitch instead, so this channel is largely dormant unless a brief is configured for direct visibility)
- `#scout-alerts` — urgent leads (score 90+, website visitors), source health alerts, match rate warnings
- `#scout-digest` — daily completion summary, weekly intelligence report

Outreach channels:
- `#pitch-approvals` — approval gate delivery. Structure every message: (1) what you need from the rep, (2) signal/context, (3) the draft or data, (4) any flags or warnings.
- `#pitch-signals` — signal notifications
- `#pitch-pipeline` — pipeline health and MEDDIC alerts
- `#pitch-quarantine` — low-confidence prospect alerts

### capability-evolver — Learning loop
Weekly analysis of: which signal types convert, which underperform, whether score thresholds need adjustment, draft approval patterns, signal dismissal patterns, engagement timing patterns. Reads conversion data from CRM, updates signal weights, voice model, and timing weights in fast-io. One llm-task call per week with `thinking: "low"`.

### taskr — Active workflow state
Tracks in-flight enrichment jobs, pending deposits, draft approvals, and signal processing state. Paired with fast-io — taskr for active state, fast-io for persistent storage.

### last30days — Reddit and community monitoring
Monitors Reddit, X, YouTube, HN for pain signals. Reddit-sourced leads convert 2-3x higher. **Security note:** run skill-vetter before production install — DataCamp flagged a prompt-injection pattern in the SKILL.md.

### skill-vetter — Pre-install security
Run against every skill before production deployment. Especially critical given this combined agent holds API keys for multiple databases plus CRM write access.

### sona-security-audit — Runtime monitoring
Ongoing monitoring of installed skills at runtime. Tracks network requests and file access. Non-optional for an agent holding enrichment database credentials and CRM write access.

### n8n-workflow — Deterministic orchestration
Backup scheduling and workflow orchestration. Primary scheduling is via OpenClaw cron, but n8n handles complex multi-step retry logic when individual API calls fail.

### attio-enhanced — CRM integration (Attio)
Read and write for contacts, companies, deals, and custom fields when Attio is the connected CRM. Handles deduplication checks, deal outcome reads for conversion analysis, and CRM writes from outreach. Batch operations support keeps API calls efficient.

### summarize — Content compression
CLI tool, not an LLM. Compresses news articles, PR announcements, and job postings into signal summaries for dossiers. Also formats audit logs and the weekly intelligence report. Zero token cost.

## Direct API Integrations (no ClawHub skill)

### Apollo.io API
Contact enrichment, email verification, intent signals. Direct REST. Read-only contact access from Pitch-mode.

### SEC EDGAR API
Public filings — 8-K, S-1, 13F. Free public API, no key needed. Regulatory signal source for finance/fintech and funding signal supplement.

### SAM.gov API
Government entities and contract vehicles. Free public API, no key needed. Primary database for government/public sector vertical.

### Candid/GuideStar API
Nonprofit financials and leadership. Free tier only — if API limit hit, fall back to SEC EDGAR 990 filings.

## Click-to-Connect Integrations

Every integration is enabled by the user through the Isol8 settings UI — never through an onboarding prompt. Only mention a missing integration when the user asks for something that requires it.

### Outbound email platform — single platform, click-to-connect
Instantly, SmartLead, Mailshake, Lemlist. **All cold sequence emails** go through the user's connected outbound platform. Never send cold outreach through gog/Gmail. When the user asks to send outreach and no outbound platform is connected:
> "To send outreach, connect your email platform in settings. Supported: Instantly, SmartLead, Mailshake, Lemlist."

The user's outbound platform handles: email sending (individual + batch), sequence/campaign management, analytics (bounce rates, open rates, reply detection), and webhook notifications for replies and unsubscribes — the user configures their platform's webhook to point at `/hooks/agent`.

Config key: `pitch-config/connected-outbound` → `{platform: "instantly" | "smartlead" | "mailshake" | "lemlist", ...credentials}`

Direct API patterns:
- Instantly: `api.instantly.ai/api/v1/...` with `$INSTANTLY_API_KEY`
- SmartLead: `server.smartlead.ai/api/v1/...` with `$SMARTLEAD_API_KEY`
- Mailshake: `api.mailshake.com/2017-04-01/...` with `$MAILSHAKE_API_KEY`
- Lemlist: `api.lemlist.com/api/...` with `$LEMLIST_API_KEY`

### CRM — single platform, click-to-connect
HubSpot, Salesforce, Attio, Pipedrive. Single record shared by both stacks (`pitch-config/connected-crm` and `scout-config/connected-crm` mirror the same credentials). When the user asks for CRM data and no CRM is connected:
> "To track prospects in your CRM, connect it in settings. Supported: HubSpot, Salesforce, Attio, Pipedrive."

Direct API patterns:
- HubSpot: `api.hubapi.com/crm/v3/objects/contacts` with Bearer `$HUBSPOT_ACCESS_TOKEN`
- Salesforce: `{instance}.salesforce.com/services/data/v59.0/sobjects` with Bearer `$SALESFORCE_ACCESS_TOKEN`
- Attio: via `attio-enhanced` skill, or `api.attio.com/v2/objects/people` with Bearer `$ATTIO_API_KEY`
- Pipedrive: `api.pipedrive.com/v1/persons` with `api_token=$PIPEDRIVE_API_TOKEN`

CRM custom fields written by the agent (on contact/deal records):
- `gdpr_consent` (boolean + timestamp + source)
- `tcpa_opt_in` (boolean + timestamp + source)
- `icp_score` (number)
- `enrichment_date` (date)
- `signal_type` (string)
- `opted_out` (boolean)

MEDDIC fields: read and update via the connected CRM's API after reply-handler MEDDIC extraction.

### Optional enterprise — click-to-connect
Available if the user brings their own subscription. Pipelines check `scout-config/connected-enterprise/{service}` — if not connected, the step is a no-op.

Enrichment:
- **ZoomInfo API** — secondary enrichment in the waterfall after Apollo. Primary enrichment for finance, manufacturing, professional services.

Intent data:
- **Bombora API** — topic-level intent. 3-5x conversion vs cold ICP.
- **6sense API** — account-level buying stage and behavioral signals.

Website visitor identification:
- **Leadfeeder or Clearbit Reveal API** — de-anonymizes companies visiting the user's site. Highest-urgency signal source — triggers `visitor-alert.lobster` via webhook immediately.

Tech stack detection:
- **BuiltWith API** / **Wappalyzer API** — tech stack detection and ecommerce platform identification.

Vertical-specific databases:
- Definitive Healthcare API — healthcare contacts and hospital data
- IQVIA API — pharma and biotech
- Bloomberg Law API — legal firm data
- CoStar API — commercial real estate
- Reonomy API — property and ownership
- Dun & Bradstreet Direct+ API — supply chain and firmographic
- Refinitiv API — financial services
- PitchBook API — PE/VC
- Cognism API — EMEA contact data
- GovWin (via agent-browser) — government opportunities

All connect via direct REST API. The vertical router determines which to query based on the active brief's vertical.

## Handling Missing Integrations

If a user asks for an action that requires a tool or service they haven't connected yet, tell them exactly which service they need to connect, list the supported options, and tell them to connect it in their settings. Do not attempt the action without the connection. Do not ask them to connect during onboarding — only when they need it.

Pattern: when a pipeline step requires a service that isn't connected, the step is a no-op and the agent logs it. If the missing service materially affects lead quality (e.g., no CRM for deposit, no enrichment source for a specific vertical), surface the gap in the daily digest or in direct conversation:
> "To do that, I need access to your [service]. You can connect [options] in your settings."

## Security: Do Not Install
- `salesforce-api` — ClawHavoc targeted (use direct API via connected CRM instead)
- `pipedrive-api` — ClawHavoc targeted
- `attio-api` — ClawHavoc targeted (use `attio-enhanced` instead)
- `linkedin-automation` — LinkedIn bans automated messaging
- Any skill claiming to send LinkedIn messages at volume
- Any skill under 30 days old on ClawHub without VirusTotal clearance
- Any skill without VirusTotal clearance

## Tools This Agent Does NOT Have Access To (for the sourcing stack)

The Scout stack is explicitly denied:
- `foxreach-io` — Scout never sends outreach (Pitch handles outreach via the connected outbound platform)
- `campaign-orchestrator` — Scout never manages sequences
- `phone-caller` — Scout never makes calls

These remain available to the Pitch stack only where appropriate. See `tools.deny` in openclaw.json for the canonical list.
