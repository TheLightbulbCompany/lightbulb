name: cookbook-refresh

# Daily 04:00 cron: refreshes OpenClaw doc cache from llms.txt index + agent snapshots from ~/.openclaw/agents/

steps:
  # Step 1 — Fetch OpenClaw doc index
  - id: fetch-index
    command: >
      exec --json --shell 'curl -sf https://docs.openclaw.ai/llms.txt | node -e "const lines=require(\"fs\").readFileSync(0,\"utf8\").split(\"\\n\").filter(l=>l.startsWith(\"https://\"));process.stdout.write(JSON.stringify({urls:lines}))"'

  # Step 2 — Diff against cache, identify new + changed pages
  - id: diff-cache
    command: >
      exec --json --shell 'echo "$fetch-index.json" | node atlas-cookbook-reader.js --action=diff-index'
    stdin: $fetch-index.json

  # Step 3 — Fetch new/changed pages (capped at 50/run to avoid traffic spikes)
  - id: fetch-pages
    command: >
      exec --json --shell 'echo "$diff-cache.json" | node atlas-cookbook-reader.js --action=fetch-pages --max-fetches=50'
    stdin: $diff-cache.json
    condition: $diff-cache.json.changed_count > 0

  # Step 4 — Update cache (writes per-page to fast-io)
  - id: update-cache
    command: >
      exec --json --shell 'echo "$fetch-pages.json" | node atlas-cookbook-reader.js --action=update-cache'
    stdin: $fetch-pages.json
    condition: $diff-cache.json.changed_count > 0

  # Step 5 — Snapshot prior agent builds from ~/.openclaw/agents/
  - id: snapshot-agents
    command: >
      exec --json --shell 'node atlas-cookbook-reader.js --action=snapshot-agents'

  # Step 6 — Update refresh-state timestamps
  - id: update-state
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-config/cookbook-refresh-state",
        "data": {
          "last_refreshed": "{{timestamp}}",
          "pages_added": $diff-cache.json.added_count,
          "pages_changed": $diff-cache.json.changed_count,
          "agents_snapshotted": $snapshot-agents.json.count
        }
      }'

  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/cookbook-refresh/daily",
        "data": {
          "action": "cookbook_refreshed",
          "pages_added": $diff-cache.json.added_count,
          "pages_changed": $diff-cache.json.changed_count,
          "agents_snapshotted": $snapshot-agents.json.count
        }
      }'
