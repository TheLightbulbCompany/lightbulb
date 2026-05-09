name: enrich-and-score
# Requirements 29-36, 38, 48, 50, 51: Waterfall enrichment, multi-contact, scoring, compliance.
# Triggered per lead after signal monitoring qualifies them.
# LLM calls: 0-1 (outreach angle for ~30% of leads that don't match deterministic map)

args:
  lead_domain:
    required: true
  signal_data:
    required: true
  brief_id:
    required: true

steps:
  # Step 1: Check enrichment cache (Req 33)
  - id: cache-check
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "enrichment-cache/{{lead_domain}}"
      }'
      | exec --json --shell 'node scripts/scout-enrichment-cache.js'

  # Step 2: Load brief for ICP criteria and vertical routing
  - id: load-brief
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "scout-briefs/active/{{brief_id}}"
      }'

  # Step 3: Get database stack from vertical router
  - id: get-stack
    command: >
      exec --json --shell 'node scripts/scout-vertical-router.js'
    stdin: $load-brief.stdout

  # Step 4: Primary database enrichment (varies by vertical)
  - id: primary-enrich
    command: >
      openclaw.invoke --tool apollo --action enrich --args-json '{
        "domain": "{{lead_domain}}",
        "find_contacts": true,
        "roles": $load-brief.json.target_roles
      }'
    # Apollo for tech/SaaS — other verticals route through enterprise click-to-connect databases or agent-browser

  # Step 5: Secondary database — ZoomInfo fill gaps
  - id: secondary-enrich
    command: >
      exec --json --shell 'curl -s "https://api.zoominfo.com/search/contact?companyDomain={{lead_domain}}" -H "Authorization: Bearer $ZOOMINFO_API_KEY"'

  # Step 6: Email verification — Apollo built-in (Req 30)
  # Apollo's enrichment response includes verification_status on email fields.
  # No dedicated verification step needed; primary-enrich already returns verification status.

  # Step 7: Recent news via Perplexity
  - id: recent-news
    command: >
      exec --json --shell 'curl -sS https://api.perplexity.ai/chat/completions \
        -H "Authorization: Bearer $PERPLEXITY_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"sonar\",\"messages\":[{\"role\":\"user\",\"content\":\"Recent news about {{lead_domain}} in the last 30 days\"}]}" \
        | node -e "const d=JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\")); const c=d.choices?.[0]?.message?.content||\"\"; const urls=d.citations||[]; process.stdout.write(JSON.stringify({results:(urls).map((u,i)=>({url:u,content:c,source:\"perplexity\"})),total:urls.length}));"'

  # Step 9: Merge enrichment with field-level tracking (Req 30, 51)
  - id: merge-enrichment
    command: >
      exec --json --shell 'node scripts/scout-enrichment-waterfall.js'
    stdin: $primary-enrich.stdout

  # Step 10: Compliance check — business email, region flagging (Req 48, 50)
  - id: compliance-check
    command: >
      exec --json --shell 'node scripts/scout-compliance-check.js'
    stdin: $merge-enrichment.stdout

  # Step 11: ICP scoring — deterministic (Req 35, 36, 38)
  - id: icp-score
    command: >
      exec --json --shell 'node scripts/scout-icp-scorer.js'
    stdin: $merge-enrichment.stdout

  # Step 12: Match rate tracking (Req 34)
  - id: match-rate
    command: >
      exec --json --shell 'node scripts/scout-match-rate-monitor.js'

  - id: match-rate-alert
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#scout-alerts",
        "text": "⚠️ Email match rate alert: $match-rate.json.diagnosis"
      }'
    condition: $match-rate.json.pass == false

  # Step 13: Build dossier (Req 42, 43)
  - id: build-dossier
    command: >
      exec --json --shell 'node scripts/scout-dossier-builder.js'
    stdin: $icp-score.stdout

  # Step 14: LLM outreach angle for unclassified signals (Req 42 — only ~30%)
  - id: llm-angle
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Suggest a specific outreach angle for this lead based on the signal. One sentence, tied directly to the trigger event.",
        "thinking": "low",
        "input": {"signal": $signal_data, "company": $merge-enrichment.json.enriched_profile},
        "schema": {
          "type": "object",
          "properties": {
            "outreach_angle": {"type": "string"},
            "signal_reference": {"type": "string"}
          },
          "required": ["outreach_angle"]
        }
      }'
    condition: $build-dossier.json.outreach_angle_needs_llm

  # Step 15: Cache enrichment result (Req 33)
  - id: cache-result
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "enrichment-cache/{{lead_domain}}",
        "data": {
          "enriched_profile": $merge-enrichment.json.enriched_profile,
          "data_sources": $merge-enrichment.json.data_sources,
          "cached_at": "{{timestamp}}"
        }
      }'

  # Step 16: Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/enrich/{{lead_domain}}",
        "data": {
          "action": "enrichment_and_scoring",
          "domain": "{{lead_domain}}",
          "brief_id": "{{brief_id}}",
          "icp_score": $icp-score.json.scored[0].score,
          "tier": $icp-score.json.scored[0].tier,
          "match_rate": $match-rate.json.match_rate,
          "compliance_flags": $compliance-check.json.flags,
          "dossier_complete": $build-dossier.json.complete,
          "cache_hit": $cache-check.json.cache_hit,
          "authorized_by": "autonomous",
          "timestamp": "{{timestamp}}"
        }
      }'
