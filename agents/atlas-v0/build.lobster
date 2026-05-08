name: build

# Phase 7 — File generation with pre-write scans + atomic write (W7 staging-dir-then-mv)
# Triggered by webhook /hooks/atlas-build-confirm AFTER architecture sign-off agentgate (S9) clears.

args:
  build_id:
    default: ""

steps:
  # Phase 7, Step 1 — ClawHavoc denylist scan (HARD per A8; build-time override available via security-exception agentgate per A14)
  - id: clawhavoc-scan
    command: >
      exec --json --shell 'node atlas-clawhavoc-enforcer.js --mode=build --build-id="{{build_id}}"'

  # Phase 7, Step 2 — Branch on scan result: pass through, or invoke security-exception agentgate (A14)
  - id: clawhavoc-decision
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{approval_channel}}",
        "text": "ClawHavoc scan flagged: $clawhavoc-scan.json.violations. Security exception requested? (verified-publisher URL required per C9)"
      }'
    condition: $clawhavoc-scan.json.pass == false
    approval: required

  # Phase 7, Step 3 — Security-exception agentgate (A14, only fires if user requests override)
  - id: security-exception
    command: >
      openclaw.invoke --tool agentgate --action confirm --args-json '{
        "gate_id": "A14_security_exception",
        "build_id": "{{build_id}}",
        "structured_reason_required": true,
        "structured_reason_options": ["legacy_data_migration", "vendor_requirement", "one_time_emergency", "other_with_explanation"],
        "free_text_min_chars_when_other": 50,
        "verified_publisher_url_required": true,
        "block_until_confirmed": true
      }'
    condition: $clawhavoc-scan.json.pass == false && $clawhavoc-decision.json.requested_override == true

  # Phase 7, Step 4 — Sensitive-data sniff (WARN per A9; audit-logged dismissal per P3)
  - id: sensitive-sniff
    command: >
      exec --json --shell 'node atlas-sensitive-sniff.js --mode=build --build-id="{{build_id}}"'

  # Phase 7, Step 5 — Surface sensitive-data warnings to user (override available)
  - id: sensitive-decision
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{approval_channel}}",
        "text": "Sensitive-data sniff found warnings: $sensitive-sniff.json.warnings. Dismiss and continue, or revise?"
      }'
    condition: $sensitive-sniff.json.warning_count > 0
    approval: required

  # Phase 7, Step 6 — File-write agentgate (S10 / A12, sync hard block; presents file list + bundle SHA256)
  - id: file-write-gate
    command: >
      openclaw.invoke --tool agentgate --action confirm --args-json '{
        "gate_id": "S10_file_write",
        "build_id": "{{build_id}}",
        "file_list": $sensitive-sniff.json.bundle_file_list,
        "bundle_sha256": $sensitive-sniff.json.bundle_sha256,
        "block_until_confirmed": true
      }'
    approval: required

  # Phase 7, Step 7 — Atomic file generation via staging-dir-then-mv (W7 contract)
  - id: file-generate
    command: >
      exec --json --shell 'node atlas-skeleton-generator.js --action=write-disk --build-id="{{build_id}}"'

  # Phase 7, Step 8 — Verify post-write file count + per-file SHA256 against staged artifact hashes
  - id: verify-write
    command: >
      exec --json --shell 'node atlas-skeleton-generator.js --action=verify-write --build-id="{{build_id}}"'

  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/build/{{build_id}}",
        "data": {
          "action": "build_complete",
          "build_id": "{{build_id}}",
          "clawhavoc_pass": $clawhavoc-scan.json.pass,
          "security_exception_used": $clawhavoc-scan.json.pass == false && $security-exception.json.confirmed,
          "sensitive_warnings_dismissed": $sensitive-sniff.json.warnings,
          "bundle_sha256": $sensitive-sniff.json.bundle_sha256,
          "file_write_confirmed": $file-write-gate.json.confirmed,
          "atomic_mv_succeeded": $verify-write.json.success
        }
      }'
