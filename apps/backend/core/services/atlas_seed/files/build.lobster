name: build

# Step 6: File generation with pre-write scans + S10 file-write agentgate + atomic write + openclaw agents add.
# Triggered by webhook /hooks/atlas-build-confirm AFTER S9 architecture sign-off clears.

args:
  build_id:
    default: ""
  agent_name:
    default: ""

steps:
  # Step 6.1 — ClawHavoc denylist scan (HARD; build-time override available via security-exception agentgate per A14)
  - id: clawhavoc-scan
    command: >
      exec --json --shell 'node atlas-clawhavoc-enforcer.js --mode=build --build-id="{{build_id}}"'

  # Step 6.2 — Branch on scan result: pass through, or invoke security-exception agentgate
  - id: clawhavoc-decision
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{approval_channel}}",
        "text": "ClawHavoc scan flagged: $clawhavoc-scan.json.violations. Security exception requested? (verified-publisher URL required)"
      }'
    condition: $clawhavoc-scan.json.pass == false
    approval: required

  # Step 6.3 — Security-exception agentgate (only fires if user requests override)
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

  # Step 6.4 — Sensitive-data sniff (WARN; audit-logged dismissal)
  - id: sensitive-sniff
    command: >
      exec --json --shell 'node atlas-sensitive-sniff.js --mode=build --build-id="{{build_id}}"'

  # Step 6.5 — Surface sensitive-data warnings to user (override available)
  - id: sensitive-decision
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{approval_channel}}",
        "text": "Sensitive-data sniff found warnings: $sensitive-sniff.json.warnings. Dismiss and continue, or revise?"
      }'
    condition: $sensitive-sniff.json.warning_count > 0
    approval: required

  # Step 6.6 — S10 file-write agentgate (sync hard block; presents file list + bundle SHA256)
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

  # Step 6.7 — Atomic file generation via staging-dir-then-mv (carries from v0)
  - id: file-generate
    command: >
      exec --json --shell 'node atlas-skeleton-generator.js --action=write-disk --build-id="{{build_id}}" --agent-name="{{agent_name}}"'

  # Step 6.8 — Verify post-write file count + per-file SHA256 against staged artifact hashes
  - id: verify-write
    command: >
      exec --json --shell 'node atlas-skeleton-generator.js --action=verify-write --build-id="{{build_id}}" --agent-name="{{agent_name}}"'

  # Step 6.9 — Explicit openclaw agents add (per ACT1 dev-unavailable resolution; no reliance on undocumented filesystem-watch)
  - id: openclaw-agents-add
    command: >
      exec --json --shell 'openclaw agents add {{agent_name}}'
    condition: $verify-write.json.success == true

  # Step 6.9.5 — Register triggers (cron rows + hook/agent-call asserts)
  - id: register-triggers
    command: >
      exec --json --shell 'node atlas-trigger-registrar.js --mode=register --build-id="{{build_id}}" --agent-name="{{agent_name}}"'
    condition: $openclaw-agents-add.json.exit_code == 0

  # Step 6.10a — Deregister triggers on rollback (cleans cron rows written this build)
  - id: deregister-triggers
    command: >
      exec --json --shell 'node atlas-trigger-registrar.js --mode=deregister --build-id="{{build_id}}" --agent-name="{{agent_name}}"'
    condition: $openclaw-agents-add.json.exit_code != 0 || ($openclaw-agents-add.json.exit_code == 0 && $register-triggers.json.success == false)

  # Step 6.10b — Rollback on agents-add OR register-triggers failure
  - id: rollback
    command: >
      exec --json --shell 'openclaw agents delete {{agent_name}} --force'
    condition: $openclaw-agents-add.json.exit_code != 0 || ($openclaw-agents-add.json.exit_code == 0 && $register-triggers.json.success == false)

  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/build/{{build_id}}",
        "data": {
          "action": "build_complete",
          "build_id": "{{build_id}}",
          "agent_name": "{{agent_name}}",
          "clawhavoc_pass": $clawhavoc-scan.json.pass,
          "security_exception_used": $clawhavoc-scan.json.pass == false && $security-exception.json.confirmed,
          "sensitive_warnings_dismissed": $sensitive-sniff.json.warnings,
          "bundle_sha256": $sensitive-sniff.json.bundle_sha256,
          "file_write_confirmed": $file-write-gate.json.confirmed,
          "atomic_mv_succeeded": $verify-write.json.success,
          "openclaw_add_succeeded": $openclaw-agents-add.json.exit_code == 0,
          "register_triggers_success": $register-triggers.json.success,
          "register_triggers_cron_count": $register-triggers.json.cron_registered_count,
          "register_triggers_hook_count": $register-triggers.json.hook_asserted_count,
          "rollback_fired": $openclaw-agents-add.json.exit_code != 0 || ($openclaw-agents-add.json.exit_code == 0 && $register-triggers.json.success == false),
          "deregister_triggers_cron_removed": $deregister-triggers.json.cron_removed
        }
      }'
