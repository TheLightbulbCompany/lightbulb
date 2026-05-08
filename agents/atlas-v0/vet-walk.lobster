name: vet-walk

# Phase 4 — Step 4 vet loop (W4, architecture addition #1: drop-resilient per-requirement persistence)
# Walker iterates the requirement matrix in order, presents each row, waits for user gate confirmation,
# persists per-requirement decision after each gate. Refuses to advance past unresolved row.

args:
  build_id:
    default: ""

steps:
  # Step 1 — Load requirement matrix (W5 hash-verification — abort on mismatch)
  - id: load-matrix
    command: >
      exec --json --shell 'node atlas-requirement-walker.js --action=load --build-id="{{build_id}}"'

  # Step 2 — Initialize taskr entries per requirement (verified invocation surface from Vera/Ora per S5)
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

  # Step 3 — Walker loop entry: present next un-vetted row to user (or resume from drop per architecture addition #1)
  - id: walker-iteration
    command: >
      exec --json --shell 'node atlas-requirement-walker.js --action=present-next --build-id="{{build_id}}"'

  # Step 4 — Present row to user with rationale, gate on response (Slack approval surface)
  - id: present-row
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "$walker-iteration.json.req_id: $walker-iteration.json.requirement_text\n\nRationale: $walker-iteration.json.rationale\n\nApprove / change (with what) / drop (with why)?"
      }'
    approval: required

  # Step 5 — Record decision per-requirement to fast-io (architecture addition #1, drop-resilient)
  - id: record-decision
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-builds/{{build_id}}/decisions/$walker-iteration.json.req_id",
        "data": {
          "req_id": "$walker-iteration.json.req_id",
          "decision": "$present-row.json.decision",
          "user_note": "$present-row.json.user_note",
          "decided_at": "{{timestamp}}",
          "presented_at": "$walker-iteration.json.presented_at"
        }
      }'

  # Step 6 — Update taskr (move req from outstanding to completed)
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

  # Step 7 — Check if more requirements outstanding; loop or finish
  - id: check-loop
    command: >
      exec --json --shell 'node atlas-requirement-walker.js --action=check-loop --build-id="{{build_id}}"'

  # Step 8 — If more outstanding, re-trigger walker (loop continues); else finalize
  - id: continue-or-finalize
    command: >
      openclaw.invoke --tool n8n-workflow --action trigger --args-json '{
        "workflow": "vet-walk-continue",
        "input": {"build_id": "{{build_id}}"}
      }'
    condition: $check-loop.json.more_outstanding == true

  # Step 9 — Finalize: emit completion summary
  - id: finalize
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "Step 4 vet complete. $check-loop.json.completed_count of $load-matrix.json.requirement_count vetted. Phase 5 recap incoming."
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
