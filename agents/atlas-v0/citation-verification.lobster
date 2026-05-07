name: citation-verification

# Quarterly cron (architecture addition #3): verification pass on every external citation in
# Atlas's own AGENTS.md and any Atlas-generated agents' AGENTS.md.
# On 2 consecutive 403/404/changed-content responses, surface stale-citation warning to user.

steps:
  # Step 1 — Enumerate all AGENTS.md files (Atlas's own + every agent in ~/.openclaw/agents/)
  - id: enumerate-agents
    command: >
      exec --json --shell 'node atlas-citation-verifier.js --action=enumerate-agents'

  # Step 2 — Extract every external citation (URLs, arxiv IDs, doc paths) from each AGENTS.md
  - id: extract-citations
    command: >
      exec --json --shell 'echo "$enumerate-agents.json" | node atlas-citation-verifier.js --action=extract-citations'
    stdin: $enumerate-agents.json

  # Step 3 — Verify each citation (WebFetch with timeout; record 200 vs 403/404/changed-content)
  - id: verify-citations
    command: >
      exec --json --shell 'echo "$extract-citations.json" | node atlas-citation-verifier.js --action=verify --quarterly'
    stdin: $extract-citations.json

  # Step 4 — Update consecutive-failure tracking (2-strike rule per architecture addition #3)
  - id: update-tracking
    command: >
      exec --json --shell 'echo "$verify-citations.json" | node atlas-citation-verifier.js --action=update-tracking'
    stdin: $verify-citations.json

  # Step 5 — Surface stale-citation warnings (citations at 2+ consecutive failures)
  - id: stale-warnings
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{build_status_channel}}",
        "text": "Quarterly citation verification: $update-tracking.json.stale_count stale citations detected.\n\nDetails: $update-tracking.json.stale_summary\n\nReview the affected AGENTS.md files and either re-verify, replace with current source, or drop per H4."
      }'
    condition: $update-tracking.json.stale_count > 0

  # Audit log (always logged, even on no-stale; quarterly cadence makes the absence-of-stale itself a signal)
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/citation-verification/quarterly",
        "data": {
          "action": "citation_verification_pass",
          "agents_checked": $enumerate-agents.json.agent_count,
          "citations_verified": $verify-citations.json.total_count,
          "passed": $verify-citations.json.passed_count,
          "failed_this_run": $verify-citations.json.failed_count,
          "stale_after_2_strikes": $update-tracking.json.stale_count
        }
      }'
