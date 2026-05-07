name: architect-proposal

# Step 3: Architecture proposal — skeleton, classifier, skill recommend, archetype synthesis

args:
  build_id:
    default: ""

steps:
  # Step 3.1 — Load research brief (required input from Step 2 per H1)
  - id: load-brief
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "atlas-builds/{{build_id}}/research-brief"
      }'

  # Step 3.2 — Load intake (locked identity from Step 1)
  - id: load-intake
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "atlas-builds/{{build_id}}/intake"
      }'

  # Step 3.3 — Generate deterministic skeleton
  - id: skeleton-generate
    command: >
      exec --json --shell 'node atlas-skeleton-generator.js --action=draft'
    stdin: { brief: $load-brief.json, intake: $load-intake.json }

  # Step 3.4 — Classify each requirement deterministic vs LLM
  - id: det-llm-classify
    command: >
      exec --json --shell 'node atlas-det-llm-classifier.js'
    stdin: $skeleton-generate.json

  # Step 3.5 — Recommend skill stack (runs skill-vetter sub-process; refuses ClawHavoc-listed skills universally)
  - id: skill-recommend
    command: >
      exec --json --shell 'node atlas-skill-recommender.js'
    stdin: { skeleton: $skeleton-generate.json, brief: $load-brief.json }

  # Step 3.6 — Archetype synthesis (1 LLM call; produces user-stories AND SOUL.md voice/tone in shared schema)
  - id: archetype-synthesis
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Phrase each requirement as a user-story (As a [agent], I should [action] so that [outcome]) and produce SOUL.md voice/tone guidance for the agent archetype.",
        "input": {"skeleton": $skeleton-generate.json, "intake": $load-intake.json, "brief": $load-brief.json},
        "schema": {"type":"object","properties":{"schema_version":{"type":"string"},"user_stories":{"type":"array"},"voice_tone":{"type":"object"}},"required":["schema_version","user_stories","voice_tone"]},
        "thinking": "low",
        "maxTokens": 2500
      }'

  # Step 3.7 — Snapshot original requirement matrix (W5 hash-verification pattern)
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
          "snapshot_at": "{{timestamp}}"
        }
      }'

  # Step 3.8 — Write SHA256 of snapshot
  - id: snapshot-sha256
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-builds/{{build_id}}/requirement-matrix.sha256",
        "data": "$snapshot-matrix.sha256"
      }'

  # Step 3.9 — Present audit table to user
  - id: present-audit
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "Step 3 audit complete. Requirement matrix snapshotted (SHA256: $snapshot-matrix.sha256). Step 4 vet starts on confirmation."
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
          "requirement_count": $skeleton-generate.json.requirement_count
        }
      }'
