name: recap-and-cost

# Phases 5 + 6 — Recap (W5) + Cost-impact estimate (W6)

args:
  build_id:
    default: ""

steps:
  # Phase 5, Step 1 — Verify SHA256 of original requirement matrix (W5 hash-verification, abort on mismatch)
  - id: verify-matrix-hash
    command: >
      exec --json --shell 'node atlas-recap-diff.js --action=verify-hash --build-id="{{build_id}}"'

  # Phase 5, Step 2 — Produce recap diff (original snapshot vs final decisions)
  - id: recap-diff
    command: >
      exec --json --shell 'node atlas-recap-diff.js --action=diff --build-id="{{build_id}}"'
    condition: $verify-matrix-hash.json.hash_matches == true

  # Phase 5, Step 3 — Persist recap
  - id: persist-recap
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-builds/{{build_id}}/recap",
        "data": $recap-diff.json
      }'

  # Phase 6, Step 1 — Count deterministic vs LLM nodes (L7 tier-agnostic architectural ratio)
  - id: cost-count
    command: >
      exec --json --shell 'node atlas-cost-counter.js --build-id="{{build_id}}"'

  # Phase 6, Step 2 — Load tier (for tier-cost-estimator)
  - id: load-tier
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "atlas-config/user-tier"
      }'

  # Phase 6, Step 3 — Tier cost estimate (L8 visual separation of what-if; L4 line-itemed markup)
  - id: tier-cost-estimate
    command: >
      exec --json --shell 'node atlas-tier-cost-estimator.js'
    stdin: { count: $cost-count.json, tier: $load-tier.json }

  # Phase 6, Step 4 — Persist cost estimate
  - id: persist-cost
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-builds/{{build_id}}/cost-estimate",
        "data": $tier-cost-estimate.json
      }'

  # Phase 6, Step 5 — Present recap + cost-impact card to user (architecture sign-off agentgate at S9 follows)
  - id: present-summary
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "Phase 5 recap + Phase 6 cost-impact ready.\n\nRecap: $recap-diff.json.summary\n\nCost-impact (PROJECTED, NOT TESTED per H5):\n- Deterministic-replacement count: $cost-count.json.replacement_count (~$cost-count.json.replacement_ratio% — archetype-specific, not cross-archetype-comparable)\n- Per-tier $ delta at $load-tier.json.tier: $tier-cost-estimate.json.delta_text\n\nProceed to Phase 7 architecture sign-off agentgate (S9)?"
      }'

  # Phase 6, Step 6 — S9 architecture sign-off agentgate (sync hard block)
  - id: architecture-signoff
    command: >
      openclaw.invoke --tool agentgate --action confirm --args-json '{
        "gate_id": "S9_architecture_signoff",
        "build_id": "{{build_id}}",
        "summary": "Architecture sign-off — proceed to Phase 7 file generation?",
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
          "tier": "$load-tier.json.tier",
          "architecture_signoff": "$architecture-signoff.json.confirmed"
        }
      }'
