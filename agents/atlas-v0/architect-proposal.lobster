name: architect-proposal

args:
  build_id:
    default: ""

steps:
  # Phase 3, Step 1 — Load research brief (required input from Phase 2 per W1)
  - id: load-brief
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "atlas-builds/{{build_id}}/research-brief"
      }'

  # Phase 3, Step 2 — Load tier (for downstream LLM call routing per L2)
  - id: load-tier
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "atlas-config/user-tier"
      }'

  # Phase 3, Step 3 — Load intake (locked identity from Phase 1)
  - id: load-intake
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "atlas-builds/{{build_id}}/intake"
      }'

  # Phase 3, Step 4 — Generate deterministic skeleton (R1 outcome-required, R4 enforce hard boundaries section)
  - id: skeleton-generate
    command: >
      exec --json --shell 'node atlas-skeleton-generator.js'
    stdin: { brief: $load-brief.json, intake: $load-intake.json }

  # Phase 3, Step 5 — Classify each requirement deterministic vs LLM (M2 15% threshold check)
  - id: det-llm-classify
    command: >
      exec --json --shell 'node atlas-det-llm-classifier.js'
    stdin: $skeleton-generate.json

  # Phase 3, Step 6 — Recommend skill stack (runs skill-vetter sub-process per S2; refuses C1-C8 per C9)
  - id: skill-recommend
    command: >
      exec --json --shell 'node atlas-skill-recommender.js'
    stdin: { skeleton: $skeleton-generate.json, brief: $load-brief.json }

  # Phase 3, Step 7 — Archetype synthesis (1 LLM call; produces R1 user-stories AND R5 SOUL.md voice/tone in shared schema per R5)
  - id: archetype-synthesis
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Phrase each requirement as a user-story (As a [agent], I should [action] so that [outcome]) and produce SOUL.md voice/tone guidance for the agent archetype. Cite the specific requirement source for every user-story.",
        "input": {"skeleton": $skeleton-generate.json, "intake": $load-intake.json, "brief": $load-brief.json},
        "schema": {"type":"object","properties":{"schema_version":{"type":"string"},"user_stories":{"type":"array","items":{"type":"object","properties":{"req_id":{"type":"string"},"agent_name":{"type":"string"},"action":{"type":"string"},"outcome":{"type":"string"}},"required":["req_id","agent_name","action","outcome"]}},"voice_tone":{"type":"object","properties":{"identity":{"type":"string"},"voice":{"type":"string"},"tone_by_context":{"type":"string"},"boundaries":{"type":"string"},"what_you_care_about":{"type":"string"}},"required":["identity","voice","tone_by_context","boundaries","what_you_care_about"]}},"required":["schema_version","user_stories","voice_tone"]},
        "thinking": "low",
        "provider": "$load-tier.json.provider",
        "model": "$load-tier.json.cheap_model",
        "maxTokens": 2500
      }'

  # Phase 3, Step 8 — Snapshot original requirement matrix (W5 hash-verification pattern, write-once intent)
  - id: snapshot-matrix
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-builds/{{build_id}}/requirement-matrix",
        "data": {
          "skeleton": $skeleton-generate.json,
          "classifications": $det-llm-classify.json,
          "skill_recommendations": $skill-recommend.json,
          "user_stories": $archetype-synthesis.json.user_stories,
          "voice_tone": $archetype-synthesis.json.voice_tone,
          "schema_version": "$archetype-synthesis.json.schema_version",
          "snapshot_at": "{{timestamp}}"
        }
      }'

  # Phase 3, Step 9 — Write SHA256 of snapshot (W5 enforcement)
  - id: snapshot-sha256
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-builds/{{build_id}}/requirement-matrix.sha256",
        "data": "$snapshot-matrix.sha256"
      }'

  # Phase 3, Step 10 — Present audit table to user (Phase 3 audit autonomous, Phase 4 vet is the gate per A2)
  - id: present-audit
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "Phase 3 audit complete. Requirement matrix snapshotted (SHA256: $snapshot-matrix.sha256). Phase 4 vet starts on confirmation."
      }'

  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/architect-proposal/{{build_id}}",
        "data": {
          "action": "architect_proposal_complete",
          "build_id": "{{build_id}}",
          "matrix_sha256": "$snapshot-matrix.sha256",
          "requirement_count": $skeleton-generate.json.requirement_count,
          "schema_version": "$archetype-synthesis.json.schema_version"
        }
      }'
