# USER.md

Holds the operator's profile and preferences. Atlas reads it at intake. Replace placeholders with your actual values.

## Identity
- name: <your name>
- role: <your role>
- company: <your company or team>
- timezone: <IANA timezone, e.g., America/New_York>
- preferred_name: <how Atlas should address you in Slack>

## Communication preferences
- approval_channel: <Slack channel for agentgate confirmations, e.g., #atlas-approvals>
- vet_channel: <Slack channel for line-by-line vet, e.g., #atlas-vet>
- build_status_channel: <Slack channel for build progress, e.g., #atlas-builds>
- digest_style: <narrative | bullet_points | numbers_first>

## Build defaults
- prefer_extend_over_separate: <true | false>  # Default when stack-overlap returns "ambiguous"
- archive_pending_submits_after_days: <integer>  # When pending-submits-prompt cron should stop reminding
- auto_publish_to_marketplace: false  # Hard-coded false; Phase 8 is opt-in only, cannot be true

## Notes
Free-text. Use this for: prior agent build conventions you want Atlas to follow, naming conventions for agents you'll generate, preferred vet cadence, anything else Atlas should know.
