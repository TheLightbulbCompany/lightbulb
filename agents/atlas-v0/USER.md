# USER.md

This file holds the user's profile and preferences. Atlas reads it at intake to tailor the build flow. Replace placeholders with your actual values.

## Identity

- name: <your name>
- role: <your role — e.g., founder, engineer, ops lead>
- company: <your company or team name>
- timezone: <IANA timezone, e.g., America/New_York>
- preferred_name: <how Atlas should address you in Slack>

## isol8 LLM tier

Atlas reads your active LLM tier from your isol8 account config (preferred via fast-io, env-var fallback per L1). This section is informational — it does NOT override the actual tier source. If the values diverge, your isol8 settings page is authoritative.

- tier: <one of: byo_openai | byo_claude | hosted_claude>
- default_model: <override only if you want non-default — Atlas defaults to cheapest tier-appropriate model per L3>

## Communication preferences

- approval_channel: <Slack channel for agentgate confirmations, e.g., #atlas-approvals>
- vet_channel: <Slack channel for Step 4 line-by-line vet, e.g., #atlas-vet>
- build_status_channel: <Slack channel for build progress, e.g., #atlas-builds>
- digest_style: <narrative | bullet_points | numbers_first>

## Build defaults

- prefer_extend_over_separate: <true | false> # When stack-overlap returns "ambiguous", default recommendation
- archive_pending_submits_after_days: <integer> # When pending-submits-prompt cron should stop reminding
- auto_publish_to_marketplace: <false> # Hard-coded false per Phase 8 opt-in-only design; cannot be true

## Notes

Free-text. Use this for: prior agent build conventions you want Atlas to follow, naming conventions for agents you'll generate, preferred vet cadence (line-by-line vs section batches), anything else Atlas should know that doesn't fit elsewhere.
