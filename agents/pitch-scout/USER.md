# User Profile

<!-- Replace with the rep / founder information -->

name: [Rep / Founder Name]
role: [Sales Rep / SDR / AE / Sales Lead / Founder]
company: [Company Name]
product: [What you sell — one line]
timezone: America/New_York
preferred_name: [How they want to be addressed]

## Sourcing Context (Scout)

primary_verticals: [e.g., "technology, finance"]
secondary_verticals: [e.g., "manufacturing"]
target_company_size: [e.g., "50-500 employees"]
target_geographies: [e.g., "US, Canada, EMEA"]
target_roles: [e.g., "VP Engineering, CTO, Head of Platform"]
daily_volume_target: [e.g., "20 priority leads/day"]

## Stack

crm: [hubspot / salesforce / attio / pipedrive / none]
outbound_platform: [instantly / smartlead / mailshake / lemlist / none]
enrichment_databases: [e.g., "apollo (always), zoominfo (optional)"]
intent_data: [bombora / 6sense / none]
visitor_identification: [leadfeeder / clearbit_reveal / none]
vertical_specific_databases: [e.g., "definitive_healthcare, costar"]

## Internal Handoff (Scout → Pitch)

pitch_installed: true
<!-- This is a combined deployment — Scout always deposits leads into Pitch's queue.  -->
<!-- Direct-to-CRM deposit is disabled in this configuration. -->

## Communication Preferences

Sourcing channels:
- Lead alerts (urgent, score 90+, visitors): Slack (#scout-alerts)
- Daily/weekly digests: Slack (#scout-digest)
- Source health alerts: Slack (#scout-alerts)
- Digest style: [signals_only / with_context / detailed]

Outreach channels:
- Approval channel: Slack (#pitch-approvals)
- Signal alerts: Slack (#pitch-signals)
- Pipeline health: Slack (#pitch-pipeline)
- Quarantine alerts: Slack (#pitch-quarantine)
- Preferred response window: [e.g., "within 2 hours during business hours"]

## Notes

<!-- Any rep-specific context the agent should know -->
<!-- e.g., "Avoid sourcing into Acme Corp — strategic partner, not a target" -->
<!-- e.g., "We only sell to companies that have raised Series A or later" -->
<!-- e.g., "Voice model lives at gdrive://voice-samples/<rep>/ — pull style cues from recent sent items" -->
