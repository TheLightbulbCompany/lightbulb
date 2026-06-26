# USER.md

Holds the operator's profile and preferences. Atlas reads it at intake. Replace placeholders with your actual values.

## Identity
- name: <your name>
- role: <your role>
- company: <your company or team>
- timezone: <IANA timezone, e.g., America/New_York>
- preferred_name: <how Atlas should address you>

## Communication preferences

These channels are notification routing only — the agent loop drains intents from
`atlas/notifications` and posts them here. (Publishing a finished agent is your
action in the marketplace seller wizard UI, not a channel.)

- approval_channel: <channel for sign-off / file-write approvals + build outcomes, e.g., #atlas-approvals>
- vet_channel: <channel for line-by-line vet + recap, e.g., #atlas-vet>
- status_channel: <channel for build progress + stale-citation warnings, e.g., #atlas-status>
- digest_style: <narrative | bullet_points | numbers_first>

## Build defaults
- prefer_extend_over_separate: <true | false>  # Default when stack-overlap returns "ambiguous"

## Notes
Free-text. Use this for: prior agent build conventions you want Atlas to follow,
naming conventions for agents you'll generate, preferred vet cadence, anything
else Atlas should know.

When a build finishes, Atlas registers the agent live and posts its `agent_id` to
your approval_channel. If you want to list it on the marketplace, open the seller
wizard and pick that agent — Atlas does not publish for you.
