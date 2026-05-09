name: prospect-enrich
# Requirements 12, 13, 14, 15, 16, 17
# Triggered per prospect after signal detection qualifies them.
# LLM calls: 1 (research brief compilation via llm-task, thinking: low)

args:
  prospect_domain:
    required: true
  signal_data:
    required: true

steps:
  # Step 1: Apollo enrichment — primary source (Req 14)
  - id: apollo-lookup
    command: >
      exec --json --shell 'curl -s "https://api.apollo.io/v1/organizations/enrich?domain={{prospect_domain}}" -H "X-Api-Key: $APOLLO_API_KEY"'

  # Step 2: Contact verification check (Req 4)
  - id: contact-verify
    command: >
      exec --json --shell 'curl -s "https://api.apollo.io/v1/people/match?domain={{prospect_domain}}&reveal_personal_emails=false" -H "X-Api-Key: $APOLLO_API_KEY"'

  # Step 3: LinkedIn/web research — primary source (Req 14)
  - id: web-research
    command: >
      openclaw.invoke --tool agent-browser --action browse --args-json '{
        "url": "https://www.linkedin.com/company/{{prospect_domain}}",
        "extract": "company_size,recent_posts,leadership,tech_stack,about"
      }'

  # Step 4: CRM history check (Req 13)
  # Click-to-connect: reads from whichever CRM is connected
  - id: load-crm-config
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "pitch-config/connected-crm"}'

  - id: crm-check-hubspot
    command: >
      exec --json --shell 'curl -s "https://api.hubapi.com/crm/v3/objects/contacts/search" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"filterGroups\":[{\"filters\":[{\"propertyName\":\"domain\",\"operator\":\"EQ\",\"value\":\"{{prospect_domain}}\"}]}],\"properties\":[\"email\",\"firstname\",\"lastname\",\"dealstage\",\"notes_last_updated\"]}"'
    condition: $load-crm-config.json.platform == "hubspot"

  - id: crm-check-salesforce
    command: >
      exec --json --shell 'curl -s "$SALESFORCE_INSTANCE_URL/services/data/v59.0/query?q=SELECT+Name,Email,StageName,LastActivityDate+FROM+Contact+WHERE+Domain__c='\''{{prospect_domain}}'\''" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN"'
    condition: $load-crm-config.json.platform == "salesforce"

  - id: crm-check-attio
    command: >
      exec --json --shell 'curl -s "https://api.attio.com/v2/objects/companies/records/query" \
        -H "Authorization: Bearer $ATTIO_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"filter\":{\"domain\":\"{{prospect_domain}}\"},\"select\":[\"contacts\",\"deals\",\"activities\",\"meddic\"]}"'
    condition: $load-crm-config.json.platform == "attio"

  - id: crm-check-pipedrive
    command: >
      exec --json --shell 'curl -s "https://api.pipedrive.com/v1/organizations/search?term={{prospect_domain}}&fields=domain&api_token=$PIPEDRIVE_API_TOKEN"'
    condition: $load-crm-config.json.platform == "pipedrive"

  - id: crm-check
    command: >
      exec --json --shell 'node -e "
        const input = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
        const data = input.hubspot || input.salesforce || input.attio || input.pipedrive || {};
        process.stdout.write(JSON.stringify(data.results || data.records || data.data || data));
      "'
    stdin: '{"hubspot": $crm-check-hubspot.json, "salesforce": $crm-check-salesforce.json, "attio": $crm-check-attio.json, "pipedrive": $crm-check-pipedrive.json}'

  # Step 5: Compile research brief — ONE LLM CALL (Req 13, 14)
  - id: compile-brief
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Compile a prospect research brief from these data sources. For each field, populate verification_flags based on which source the data came from. Apollo and LinkedIn are primary sources. Web scrape data is secondary. If a field cannot be populated from any source, set it to null with verification status unverified. Never fabricate data to fill gaps.",
        "thinking": "low",
        "input": {
          "apollo": {"_source": "primary", "data": $apollo-lookup.json},
          "linkedin": {"_source": "primary", "data": $web-research.json},
          "crm": {"_source": "primary", "data": $crm-check.json},
          "signal": $signal_data
        },
        "schema": {
          "type": "object",
          "properties": {
            "domain": {"type": "string"},
            "company_size": {"type": ["number", "null"]},
            "funding_stage": {"type": ["string", "null"]},
            "estimated_revenue": {"type": ["string", "null"]},
            "tech_stack": {"type": ["array", "null"], "items": {"type": "string"}},
            "trigger_signal": {
              "type": "object",
              "properties": {
                "type": {"type": "string"},
                "context": {"type": "string"},
                "qualification_reason": {"type": "string"}
              },
              "required": ["type", "context"]
            },
            "prospect_role": {"type": ["string", "null"]},
            "prospect_tenure": {"type": ["string", "null"]},
            "recent_public_content": {
              "type": ["array", "null"],
              "items": {
                "type": "object",
                "properties": {
                  "title": {"type": "string"},
                  "summary": {"type": "string"},
                  "date": {"type": "string"}
                }
              }
            },
            "crm_history": {
              "type": ["array", "null"],
              "items": {
                "type": "object",
                "properties": {
                  "date": {"type": "string"},
                  "type": {"type": "string"},
                  "outcome": {"type": "string"},
                  "contact": {"type": "string"}
                }
              }
            },
            "competitive_context": {
              "type": ["object", "null"],
              "properties": {
                "current_vendor": {"type": ["string", "null"]},
                "evaluation_status": {"type": ["string", "null"]},
                "public_sentiment": {"type": ["string", "null"]}
              }
            },
            "verification_flags": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "field": {"type": "string"},
                  "source_tier": {"type": "string", "enum": ["primary", "secondary", "unverified"]}
                },
                "required": ["field", "source_tier"]
              }
            }
          },
          "required": ["domain", "trigger_signal", "verification_flags"]
        }
      }'

  # Step 6: Enrichment confidence check (Req 15)
  - id: confidence-check
    command: >
      exec --json --shell 'node scripts/pitch-enrichment-confidence.js'
    stdin: $compile-brief.stdout

  # Step 7: ICP scoring — deterministic (Req 16)
  - id: icp-score
    command: >
      exec --json --shell 'node scripts/pitch-icp-router.js'
    stdin: $confidence-check.stdout
    condition: $confidence-check.json.pass

  # Step 8: Persist brief to fast-io
  - id: persist-brief
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "$icp-score.json.destination",
        "data": {
          "brief": $compile-brief.json,
          "icp_score": $icp-score.json.score,
          "qualified": $icp-score.json.qualified,
          "breakdown": $icp-score.json.breakdown,
          "enrichment_confidence": $confidence-check.json.confidence
        }
      }'

  # Step 9: CRM sync (Req 17)
  # Click-to-connect: writes to whichever CRM is connected
  - id: crm-sync-hubspot
    command: >
      exec --json --shell 'curl -s -X PATCH "https://api.hubapi.com/crm/v3/objects/contacts/{{prospect_domain}}" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"properties\":{\"icp_score\":$icp-score.json.score,\"enrichment_date\":\"{{timestamp}}\",\"signal_type\":\"$compile-brief.json.trigger_signal.type\",\"enrichment_confidence\":$confidence-check.json.confidence}}"'
    condition: $load-crm-config.json.platform == "hubspot"

  - id: crm-sync-salesforce
    command: >
      exec --json --shell 'curl -s -X PATCH "$SALESFORCE_INSTANCE_URL/services/data/v59.0/sobjects/Contact/{{prospect_domain}}" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"ICP_Score__c\":$icp-score.json.score,\"Enrichment_Date__c\":\"{{timestamp}}\",\"Signal_Type__c\":\"$compile-brief.json.trigger_signal.type\",\"Enrichment_Confidence__c\":$confidence-check.json.confidence}"'
    condition: $load-crm-config.json.platform == "salesforce"

  - id: crm-sync-attio
    command: >
      exec --json --shell 'curl -s -X PATCH "https://api.attio.com/v2/objects/companies/records/{{prospect_domain}}" \
        -H "Authorization: Bearer $ATTIO_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"icp_score\":$icp-score.json.score,\"enrichment_date\":\"{{timestamp}}\",\"signal_type\":\"$compile-brief.json.trigger_signal.type\",\"enrichment_confidence\":$confidence-check.json.confidence}"'
    condition: $load-crm-config.json.platform == "attio"

  - id: crm-sync-pipedrive
    command: >
      exec --json --shell 'curl -s -X PUT "https://api.pipedrive.com/v1/persons/{{prospect_domain}}?api_token=$PIPEDRIVE_API_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"icp_score\":$icp-score.json.score,\"enrichment_date\":\"{{timestamp}}\",\"signal_type\":\"$compile-brief.json.trigger_signal.type\"}"'
    condition: $load-crm-config.json.platform == "pipedrive"

  # Step 10: Quarantine notification if confidence too low (Req 15)
  - id: quarantine-notify
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#pitch-quarantine",
        "text": "Prospect {{prospect_domain}} quarantined. Confidence: $confidence-check.json.confidence. Missing fields: $confidence-check.json.remediation.missing_fields"
      }'
    condition: $confidence-check.json.pass == false

  # Step 11: Audit log (Req 38)
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/enrich/{{prospect_domain}}",
        "data": {
          "action": "prospect_enrichment",
          "domain": "{{prospect_domain}}",
          "icp_score": $icp-score.json.score,
          "qualified": $icp-score.json.qualified,
          "enrichment_confidence": $confidence-check.json.confidence,
          "quarantined": $confidence-check.json.pass == false,
          "authorized_by": "autonomous",
          "timestamp": "{{timestamp}}"
        }
      }'
