name: recap-and-cost

# Step 5: Recap (W5 SHA256 hash verification) + Cost-impact estimate + S9 architecture sign-off agentgate

args:
  build_id:
    default: ""

steps:
  # Step 5.1 — Verify SHA256 of original requirement matrix (abort on mismatch per W5)
  - id: verify-matrix-hash
    command: >
      exec --json --shell 'node atlas-recap-diff.js --action=verify-hash --build-id="{{build_id}}"'

  # Step 5.2 — Produce recap diff (original snapshot vs final decisions)
  - id: recap-diff
    command: >
      exec --json --shell 'node atlas-recap-diff.js --action=diff --build-id="{{build_id}}"'
    condition: $verify-matrix-hash.json.hash_matches == true

  # Step 5.3 — Persist recap
  - id: persist-recap
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-builds/{{build_id}}/recap",
        "data": $recap-diff.json
      }'

  # Step 5.4 — Count deterministic vs LLM nodes (architectural ratio per L7)
  - id: cost-count
    command: >
      exec --json --shell 'node atlas-cost-counter.js --build-id="{{build_id}}"'

  # Step 5.5 — Persist cost-count
  - id: persist-cost
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-builds/{{build_id}}/cost-estimate",
        "data": $cost-count.json
      }'

  # Step 5.6 — Present recap + cost-impact card to user
  - id: present-summary
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "Step 5 recap + cost-impact ready.\n\nRecap: $recap-diff.json.summary\n\nCost-impact (PROJECTED, NOT TESTED per H5):\n- Deterministic-replacement count: $cost-count.json.replacement_count (~$cost-count.json.replacement_ratio% — archetype-specific, not cross-archetype-comparable)\n\nProceed to Step 6 architecture sign-off agentgate (S9)?"
      }'

  # Step 5.7 — S9 architecture sign-off agentgate (sync hard block)
  - id: architecture-signoff
    command: >
      openclaw.invoke --tool agentgate --action confirm --args-json '{
        "gate_id": "S9_architecture_signoff",
        "build_id": "{{build_id}}",
        "summary": "Architecture sign-off — proceed to Step 6 file generation?",
        "block_until_confirmed": true
      }'
    approval: required

  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/recap-and-cost/{{build_id}}",
        "data": {
          "action": "recap_and_cost_complete",
          "build_id": "{{build_id}}",
          "matrix_hash_verified": $verify-matrix-hash.json.hash_matches,
          "replacement_ratio": $cost-count.json.replacement_ratio,
          "architecture_signoff": "$architecture-signoff.json.confirmed"
        }
      }'
