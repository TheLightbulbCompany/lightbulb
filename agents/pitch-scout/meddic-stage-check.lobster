name: meddic-stage-check
# Requirement 36: Surface gaps at moment of stage change.
# Triggered by Attio webhook via OpenClaw POST /hooks/agent.
# LLM calls: 0 (fully deterministic)

args:
  prospect_domain:
    required: true
  new_stage:
    required: true
  old_stage:
    default: ""

steps:
  # Step 1: Load MEDDIC state for this deal
  # Click-to-connect: reads from whichever CRM is connected
  - id: load-crm-config
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "pitch-config/connected-crm"}'

  - id: load-meddic-hubspot
    command: >
      exec --json --shell 'curl -s "https://api.hubapi.com/crm/v3/objects/deals/search" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"filterGroups\":[{\"filters\":[{\"propertyName\":\"domain\",\"operator\":\"EQ\",\"value\":\"{{prospect_domain}}\"}]}],\"properties\":[\"meddic_metrics\",\"meddic_economic_buyer\",\"meddic_decision_criteria\",\"meddic_decision_process\",\"meddic_identify_pain\",\"meddic_champion\"]}"'
    condition: $load-crm-config.json.platform == "hubspot"

  - id: load-meddic-salesforce
    command: >
      exec --json --shell 'curl -s "$SALESFORCE_INSTANCE_URL/services/data/v59.0/query?q=SELECT+MEDDIC_Metrics__c,MEDDIC_Economic_Buyer__c,MEDDIC_Decision_Criteria__c,MEDDIC_Decision_Process__c,MEDDIC_Identify_Pain__c,MEDDIC_Champion__c+FROM+Opportunity+WHERE+Domain__c='\''{{prospect_domain}}'\''" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN"'
    condition: $load-crm-config.json.platform == "salesforce"

  - id: load-meddic-attio
    command: >
      exec --json --shell 'curl -s "https://api.attio.com/v2/objects/companies/records/query" \
        -H "Authorization: Bearer $ATTIO_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"filter\":{\"domain\":\"{{prospect_domain}}\"},\"select\":[\"meddic\"]}"'
    condition: $load-crm-config.json.platform == "attio"

  - id: load-meddic-pipedrive
    command: >
      exec --json --shell 'curl -s "https://api.pipedrive.com/v1/deals?domain={{prospect_domain}}&api_token=$PIPEDRIVE_API_TOKEN"'
    condition: $load-crm-config.json.platform == "pipedrive"

  - id: load-meddic
    command: >
      exec --json --shell 'node -e "
        const input = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
        const data = input.hubspot || input.salesforce || input.attio || input.pipedrive || {};
        const records = data.results || data.records || data.data || [data];
        process.stdout.write(JSON.stringify(records[0] || {}));
      "'
    stdin: '{"hubspot": $load-meddic-hubspot.json, "salesforce": $load-meddic-salesforce.json, "attio": $load-meddic-attio.json, "pipedrive": $load-meddic-pipedrive.json}'

  # Step 2: Check gaps against new stage requirements
  - id: gap-check
    command: >
      exec --json --shell 'node scripts/pitch-meddic-gap-check.js'
    stdin: '{"deal": {"domain": "{{prospect_domain}}", "stage": "{{new_stage}}", "meddic": $load-meddic.json}}'

  # Step 3: Alert if gaps exist
  - id: alert
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#pitch-pipeline",
        "text": "⚠️ STAGE CHANGE: {{prospect_domain}} moving from {{old_stage}} to {{new_stage}}.\n\n$gap-check.json.summary\n\nGaps: $gap-check.json.gaps"
      }'
    condition: $gap-check.json.has_gaps

  # Step 4: Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/stage-change/{{prospect_domain}}",
        "data": {
          "action": "meddic_stage_check",
          "domain": "{{prospect_domain}}",
          "old_stage": "{{old_stage}}",
          "new_stage": "{{new_stage}}",
          "has_gaps": $gap-check.json.has_gaps,
          "gaps": $gap-check.json.gaps,
          "authorized_by": "autonomous",
          "timestamp": "{{timestamp}}"
        }
      }'
