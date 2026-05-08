name: intake-and-research

args:
  user_request:
    default: ""
  build_id:
    default: ""

steps:
  # Phase 1, Step 1 — Identity lock (H3 enforcement, immutable record + SHA256)
  - id: identity-lock
    command: >
      exec --json --shell 'echo "{{user_request}}" | node atlas-identity-lock.js --build-id="{{build_id}}"'

  # Phase 1, Step 2 — Tier detection (L1 read-both pattern, hard-fail with settings link per L5)
  - id: tier-detect
    command: >
      exec --json --shell 'node atlas-tier-detector.js'

  # Phase 1, Step 3 — Stack overlap (B4 structured-fields-only, A6 no prose-parsing fallback)
  - id: stack-overlap
    command: >
      exec --json --shell 'echo "$identity-lock.json" | node atlas-stack-overlap.js'
    stdin: $identity-lock.json

  # Phase 1, Step 4 — Conditional overlap-disambig llm-task (B5 — only when overlap_score in 30-70 band)
  # Per-tier provider+model override per L2 — uses tier from $tier-detect.json.tier
  - id: overlap-disambig
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Classify capability overlap between proposed agent and conflicting existing agents. Return recommendation: extend, separate, or no_overlap.",
        "input": $stack-overlap.json,
        "schema": {"type":"object","properties":{"recommendation":{"type":"string","enum":["extend","separate","no_overlap"]},"confidence":{"type":"number"},"reasoning":{"type":"string"}},"required":["recommendation","confidence","reasoning"]},
        "thinking": "off",
        "provider": "$tier-detect.json.provider",
        "model": "$tier-detect.json.cheap_model",
        "maxTokens": 400
      }'
    condition: $stack-overlap.json.overlap_score >= 30 && $stack-overlap.json.overlap_score <= 70

  # Phase 1, Step 5 — Surface stack-overlap recommendation to user before Phase 2
  - id: surface-overlap
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "Stack overlap check: $stack-overlap.json.recommendation. Proceed to research?"
      }'
    approval: required

  # Phase 2, Step 1 — Cookbook read (D2 source-preference, both sources combined per D1)
  - id: cookbook-read
    command: >
      exec --json --shell 'echo "$identity-lock.json" | node atlas-cookbook-reader.js'
    stdin: $identity-lock.json

  # Phase 2, Step 2 — Failure-case routing (R2; refuses any unverified-source case per H4)
  - id: failure-case-route
    command: >
      exec --json --shell 'echo "$identity-lock.json" | node atlas-failure-case-router.js'
    stdin: $identity-lock.json

  # Phase 2, Step 3 — Compress cookbook reads (no-LLM via summarize CLI per S7)
  - id: compress-cookbook
    command: >
      openclaw.invoke --tool summarize --action compress --args-json '{
        "input": $cookbook-read.json,
        "format": "research-context"
      }'

  # Phase 2, Step 4 — Compile research brief (1 LLM call, thinking: low; tier-aware override per L2)
  - id: compile-research-brief
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Synthesize cookbook reads and failure cases into a research brief for the proposed agent. Cite specific file paths and doc URLs for every claim.",
        "input": {"cookbook": $compress-cookbook.json, "failure_cases": $failure-case-route.json, "intake": $identity-lock.json},
        "schema": {"type":"object","properties":{"summary":{"type":"string"},"relevant_analogs":{"type":"array"},"applicable_failure_cases":{"type":"array"},"open_questions":{"type":"array"}},"required":["summary","relevant_analogs","applicable_failure_cases","open_questions"]},
        "thinking": "low",
        "provider": "$tier-detect.json.provider",
        "model": "$tier-detect.json.cheap_model",
        "maxTokens": 2000
      }'

  # Phase 2, Step 5 — Persist research brief (Phase 3 reads this; pipeline aborts on absence per W1)
  - id: persist-brief
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-builds/{{build_id}}/research-brief",
        "data": $compile-research-brief.json
      }'

  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/intake-and-research/{{build_id}}",
        "data": {
          "action": "intake_and_research_complete",
          "build_id": "{{build_id}}",
          "tier_source": "$tier-detect.json.source",
          "overlap_recommendation": "$stack-overlap.json.recommendation",
          "research_brief_hash": "$compile-research-brief.sha256"
        }
      }'
