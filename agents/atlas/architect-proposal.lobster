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

  # Step 3.6 — Archetype synthesis (1 LLM call; produces user-stories,
  # voice/tone, AND per-requirement triggers[] binding in the same response.
  # No new LLM call — triggers ride on the existing multi-field-output schema.
  #
  # Schema notes for the LLM:
  #   requirements_with_triggers[i].req_id           — matches user_stories[].req_id
  #   requirements_with_triggers[i].classification   — "deterministic" | "llm" | "hybrid"
  #                                                    (echo the classifier's assignment)
  #   requirements_with_triggers[i].triggers[]       — array of triggers; for `deterministic`
  #                                                    classification MUST be non-empty
  #   trigger kinds:
  #     cron       — {kind, schedule, tz?, pipeline}   — pipeline ends in .lobster
  #     hook       — {kind, route, pipeline}            — route starts with /hooks/
  #     agent-call — {kind, callable_as, pipeline|null}
  #   `channel` kind is NOT supported in v1.
  - id: archetype-synthesis
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "For each requirement: (1) phrase as user-story (As a [agent], I should [action] so that [outcome]); (2) propose triggers[] binding the requirement to runtime entry points. Triggers MUST be non-empty when classification is `deterministic`. Trigger kinds: cron (schedule + .lobster pipeline), hook (/hooks/ route + .lobster pipeline), agent-call (callable_as + optional .lobster pipeline). Also produce SOUL.md voice/tone for the archetype.",
        "input": {"skeleton": $skeleton-generate.json, "classifications": $det-llm-classify.json, "intake": $load-intake.json, "brief": $load-brief.json},
        "schema": {"type":"object","properties":{"schema_version":{"type":"string"},"user_stories":{"type":"array"},"voice_tone":{"type":"object"},"requirements_with_triggers":{"type":"array","items":{"type":"object","properties":{"req_id":{"type":"string"},"classification":{"type":"string","enum":["deterministic","llm","hybrid"]},"triggers":{"type":"array","items":{"type":"object","properties":{"kind":{"type":"string","enum":["cron","hook","agent-call"]},"schedule":{"type":"string"},"tz":{"type":"string"},"route":{"type":"string"},"callable_as":{"type":"string"},"pipeline":{"type":["string","null"]}},"required":["kind"]}}},"required":["req_id","classification","triggers"]}}},"required":["schema_version","user_stories","voice_tone","requirements_with_triggers"]},
        "thinking": "low",
        "maxTokens": 3500
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
          "requirements_with_triggers": $archetype-synthesis.json.requirements_with_triggers,
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
