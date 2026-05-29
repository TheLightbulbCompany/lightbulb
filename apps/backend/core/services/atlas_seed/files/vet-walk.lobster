name: vet-walk

# Step 4: Vet loop. Walker iterates requirement matrix, presents each row, persists decision after each gate.
# Drop-resilient resume from next un-vetted row.

args:
  build_id:
    default: ""

steps:
  # Step 4.1 — Load requirement matrix (verifies SHA256, aborts on mismatch)
  - id: load-matrix
    command: >
      exec --json --shell 'node atlas-requirement-walker.js --action=load --build-id="{{build_id}}"'

  # Step 4.2 — Initialize taskr entries per requirement
  - id: init-taskr
    command: >
      openclaw.invoke --tool taskr --action create --args-json '{
        "key": "atlas-vet/{{build_id}}",
        "data": {
          "total_requirements": $load-matrix.json.requirement_count,
          "outstanding": $load-matrix.json.requirement_ids,
          "completed": [],
          "started_at": "{{timestamp}}"
        }
      }'

  # Step 4.3 — Walker loop: present next un-vetted row (or resume from drop)
  - id: walker-iteration
    command: >
      exec --json --shell 'node atlas-requirement-walker.js --action=present-next --build-id="{{build_id}}"'

  # Step 4.4 — Present row to user with rationale + triggers, gate on response.
  # When classification == "deterministic" the user MUST either approve the
  # triggers as drafted, edit them (change kind, schedule, route, pipeline),
  # or reclassify the requirement to llm/hybrid (which removes the trigger
  # obligation). S9.5 will refuse to advance the build otherwise.
  - id: present-row
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "$walker-iteration.json.req_id ($walker-iteration.json.classification): $walker-iteration.json.requirement_text\n\nRationale: $walker-iteration.json.rationale\n\nTriggers (deterministic-binding):\n$walker-iteration.json.triggers\n\nApprove / change (triggers and/or classification) / drop (with why)?"
      }'
    approval: required

  # Step 4.5 — Record decision per-requirement (now also accepts trigger + classification edits)
  - id: record-decision
    command: >
      exec --json --shell 'node atlas-requirement-walker.js --action=apply-trigger-edits --build-id="{{build_id}}" --req-id="$walker-iteration.json.req_id"'
    stdin: { decision: $present-row.json.decision, user_note: $present-row.json.user_note, triggers: $present-row.json.triggers, classification_override: $present-row.json.classification_override }

  # Step 4.5b — Persist the normalized decision row.
  - id: persist-decision
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-builds/{{build_id}}/decisions/$walker-iteration.json.req_id",
        "data": $record-decision.json
      }'

  # Step 4.6 — Update taskr (move req from outstanding to completed)
  - id: update-taskr
    command: >
      openclaw.invoke --tool taskr --action update --args-json '{
        "key": "atlas-vet/{{build_id}}",
        "data": {
          "outstanding_decrement": "$walker-iteration.json.req_id",
          "completed_append": {
            "req_id": "$walker-iteration.json.req_id",
            "decision": "$present-row.json.decision",
            "decided_at": "{{timestamp}}"
          }
        }
      }'

  # Step 4.7 — Check if more requirements outstanding; loop or finish
  - id: check-loop
    command: >
      exec --json --shell 'node atlas-requirement-walker.js --action=check-loop --build-id="{{build_id}}"'

  # Step 4.8 — If more outstanding, re-trigger walker
  - id: continue
    command: >
      openclaw.invoke --tool n8n-workflow --action trigger --args-json '{
        "workflow": "vet-walk-continue",
        "input": {"build_id": "{{build_id}}"}
      }'
    condition: $check-loop.json.more_outstanding == true

  # Step 4.9 — Finalize: emit completion summary
  - id: finalize
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "Step 4 vet complete. $check-loop.json.completed_count of $load-matrix.json.requirement_count vetted. Step 5 recap incoming."
      }'
    condition: $check-loop.json.more_outstanding == false

  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/vet-walk/{{build_id}}",
        "data": {
          "action": "vet_iteration_complete",
          "build_id": "{{build_id}}",
          "req_id": "$walker-iteration.json.req_id",
          "decision": "$present-row.json.decision"
        }
      }'
