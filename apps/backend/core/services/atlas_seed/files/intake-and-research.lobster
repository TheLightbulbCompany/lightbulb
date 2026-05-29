name: intake-and-research

# Steps 1 + 2: Intake (identity lock, stack overlap) + Research (cookbook + failure cases + brief synthesis)

args:
  user_request:
    default: ""
  build_id:
    default: ""

steps:
  # Step 1.1 — Identity lock (H3 enforcement, immutable record + SHA256, ATLAS1 regex check)
  - id: identity-lock
    command: >
      exec --json --shell 'echo "{{user_request}}" | node atlas-identity-lock.js --build-id="{{build_id}}"'

  # Step 1.2 — Stack overlap (structured-fields-only per B4)
  - id: stack-overlap
    command: >
      exec --json --shell 'echo "$identity-lock.json" | node atlas-stack-overlap.js'
    stdin: $identity-lock.json

  # Step 1.3 — Conditional overlap-disambig llm-task (only when overlap_score in 30-70 band)
  - id: overlap-disambig
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Classify capability overlap between proposed agent and conflicting existing agents. Return recommendation: extend, separate, or no_overlap.",
        "input": $stack-overlap.json,
        "schema": {"type":"object","properties":{"recommendation":{"type":"string","enum":["extend","separate","no_overlap"]},"confidence":{"type":"number"},"reasoning":{"type":"string"}},"required":["recommendation","confidence","reasoning"]},
        "thinking": "off",
        "maxTokens": 400
      }'
    condition: $stack-overlap.json.overlap_score >= 30 && $stack-overlap.json.overlap_score <= 70

  # Step 1.4 — Surface stack-overlap recommendation to user before Step 2
  - id: surface-overlap
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "Stack overlap check: $stack-overlap.json.recommendation. Proceed to research?"
      }'
    approval: required

  # Step 2.1 — Cookbook read (two sources combined per source-preference table)
  - id: cookbook-read
    command: >
      exec --json --shell 'echo "$identity-lock.json" | node atlas-cookbook-reader.js'
    stdin: $identity-lock.json

  # Step 2.2 — Failure-case routing (refuses any unverified-source case per H4)
  - id: failure-case-route
    command: >
      exec --json --shell 'echo "$identity-lock.json" | node atlas-failure-case-router.js'
    stdin: $identity-lock.json

  # Step 2.3 — Compress cookbook reads (no-LLM via summarize CLI)
  - id: compress-cookbook
    command: >
      openclaw.invoke --tool summarize --action compress --args-json '{
        "input": $cookbook-read.json,
        "format": "research-context"
      }'

  # Step 2.4 — Compile research brief (1 LLM call, thinking: low)
  - id: compile-research-brief
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Synthesize cookbook reads and failure cases into a research brief for the proposed agent. Cite specific file paths and doc URLs for every claim.",
        "input": {"cookbook": $compress-cookbook.json, "failure_cases": $failure-case-route.json, "intake": $identity-lock.json},
        "schema": {"type":"object","properties":{"summary":{"type":"string"},"relevant_analogs":{"type":"array"},"applicable_failure_cases":{"type":"array"},"open_questions":{"type":"array"}},"required":["summary","relevant_analogs","applicable_failure_cases","open_questions"]},
        "thinking": "low",
        "maxTokens": 2000
      }'

  # Step 2.5 — Persist research brief (Step 3 reads this; pipeline aborts on absence per H1)
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
          "overlap_recommendation": "$stack-overlap.json.recommendation",
          "research_brief_hash": "$compile-research-brief.sha256"
        }
      }'
