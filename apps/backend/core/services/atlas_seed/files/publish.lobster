name: publish

# Step 7: Optional marketplace publish (user opt-in only after Step 6 completes).
# Triggered by webhook /hooks/atlas-publish-confirm. Hard refuses per Marketplace Boundary Contract.

args:
  build_id:
    default: ""
  agent_name:
    default: ""

steps:
  # Step 7.1 — ClawHavoc scan (HARD; NO override at marketplace boundary per Marketplace Boundary Contract)
  - id: clawhavoc-scan
    command: >
      exec --json --shell 'node atlas-clawhavoc-enforcer.js --mode=publish --agent-name="{{agent_name}}"'

  # Step 7.2 — Hard abort on ClawHavoc hit (no override path at publish time)
  - id: clawhavoc-abort
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{approval_channel}}",
        "text": "Publish refused: ClawHavoc denylist hit. Violations: $clawhavoc-scan.json.violations. No override at marketplace boundary per Marketplace Boundary Contract. Resolve at build-time and re-attempt publish."
      }'
    condition: $clawhavoc-scan.json.pass == false

  # Step 7.3 — Sensitive-data sniff (WARN; override available with audit log)
  - id: sensitive-sniff
    command: >
      exec --json --shell 'node atlas-sensitive-sniff.js --mode=publish --agent-name="{{agent_name}}"'
    condition: $clawhavoc-scan.json.pass == true

  # Step 7.4 — Surface warnings (override available with explicit dismissal)
  - id: sensitive-decision
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{approval_channel}}",
        "text": "Pre-publish sensitive-data warnings: $sensitive-sniff.json.warnings. Override and publish anyway? (will be audit-logged)"
      }'
    condition: $clawhavoc-scan.json.pass == true && $sensitive-sniff.json.warning_count > 0
    approval: required

  # Step 7.5 — Generate listing card (zero-LLM deterministic extraction)
  - id: listing-build
    command: >
      exec --json --shell 'node atlas-listing-builder.js --build-id="{{build_id}}" --agent-name="{{agent_name}}"'
    condition: $clawhavoc-scan.json.pass == true

  # Step 7.6 — Re-compute bundle SHA256 (continuity check vs S10 hash per Marketplace Boundary Contract)
  - id: bundle-rehash
    command: >
      exec --json --shell 'node atlas-listing-builder.js --action=bundle-hash --agent-name="{{agent_name}}"'
    condition: $clawhavoc-scan.json.pass == true

  # Step 7.7 — Hash continuity check (S10 hash vs S11 hash; HARD REFUSE on divergence per Marketplace Boundary Contract)
  - id: continuity-check
    command: >
      exec --json --shell 'node atlas-listing-builder.js --action=continuity-check --build-id="{{build_id}}" --current-hash="$bundle-rehash.json.sha256"'
    condition: $clawhavoc-scan.json.pass == true

  # Step 7.8 — S11 marketplace-submit agentgate (presents listing + hash for re-confirmation)
  - id: submit-gate
    command: >
      openclaw.invoke --tool agentgate --action confirm --args-json '{
        "gate_id": "S11_marketplace_submit",
        "build_id": "{{build_id}}",
        "listing": $listing-build.json,
        "bundle_sha256": $bundle-rehash.json.sha256,
        "block_until_confirmed": true
      }'
    condition: $continuity-check.json.continuity_holds == true
    approval: required

  # Step 7.9 — Marketplace submit (stubbed; writes payload + local preview file)
  - id: marketplace-submit
    command: >
      exec --json --shell 'node atlas-marketplace-submit.js --build-id="{{build_id}}" --agent-name="{{agent_name}}"'
    condition: $submit-gate.json.confirmed == true
    stdin: { listing: $listing-build.json, bundle_sha256: $bundle-rehash.json.sha256 }

  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/publish/{{build_id}}",
        "data": {
          "action": "publish_complete",
          "build_id": "{{build_id}}",
          "agent_name": "{{agent_name}}",
          "clawhavoc_pass": $clawhavoc-scan.json.pass,
          "sensitive_dismissals": $sensitive-sniff.json.warnings,
          "continuity_holds": $continuity-check.json.continuity_holds,
          "submit_confirmed": $submit-gate.json.confirmed,
          "submit_payload_path": "$marketplace-submit.json.preview_file_path"
        }
      }'
