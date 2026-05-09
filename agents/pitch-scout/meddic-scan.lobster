name: meddic-scan
# Requirements 35, 36, 37, 39
# Runs daily at 8 AM on cron.
# LLM calls: 0 (fully deterministic)

steps:
  # Step 1: Load all active deals with MEDDIC state
  # Click-to-connect: reads from whichever CRM is connected
  - id: load-crm-config
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "pitch-config/connected-crm"}'

  - id: load-deals-hubspot
    command: >
      exec --json --shell 'curl -s "https://api.hubapi.com/crm/v3/objects/deals?limit=100&properties=dealstage,meddic_metrics,meddic_economic_buyer,meddic_decision_criteria,meddic_decision_process,meddic_identify_pain,meddic_champion,hs_lastmodifieddate" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN"'
    condition: $load-crm-config.json.platform == "hubspot"

  - id: load-deals-salesforce
    command: >
      exec --json --shell 'curl -s "$SALESFORCE_INSTANCE_URL/services/data/v59.0/query?q=SELECT+Name,StageName,Domain__c,LastActivityDate,MEDDIC_Metrics__c,MEDDIC_Economic_Buyer__c,MEDDIC_Decision_Criteria__c,MEDDIC_Decision_Process__c,MEDDIC_Identify_Pain__c,MEDDIC_Champion__c+FROM+Opportunity+WHERE+IsClosed=false" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN"'
    condition: $load-crm-config.json.platform == "salesforce"

  - id: load-deals-attio
    command: >
      exec --json --shell 'curl -s "https://api.attio.com/v2/objects/companies/records/query" \
        -H "Authorization: Bearer $ATTIO_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"filter\":{\"pipeline\":\"active\"},\"select\":[\"meddic\",\"stage\",\"contacts\",\"last_activity_date\"]}"'
    condition: $load-crm-config.json.platform == "attio"

  - id: load-deals-pipedrive
    command: >
      exec --json --shell 'curl -s "https://api.pipedrive.com/v1/deals?status=open&api_token=$PIPEDRIVE_API_TOKEN"'
    condition: $load-crm-config.json.platform == "pipedrive"

  - id: load-deals
    command: >
      exec --json --shell 'node -e "
        const input = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
        const data = input.hubspot || input.salesforce || input.attio || input.pipedrive || {};
        const deals = data.results || data.records || data.data || [];
        process.stdout.write(JSON.stringify({deals}));
      "'
    stdin: '{"hubspot": $load-deals-hubspot.json, "salesforce": $load-deals-salesforce.json, "attio": $load-deals-attio.json, "pipedrive": $load-deals-pipedrive.json}'

  - id: no-crm-notice
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#pitch-pipeline",
        "text": "To track deals and MEDDIC data, connect your CRM in settings. Supported: HubSpot, Salesforce, Attio, Pipedrive."
      }'
    condition: $load-crm-config.json.platform == null

  # Step 2: Check MEDDIC gaps — deterministic (Req 36, 37)
  - id: scan-gaps
    command: >
      exec --json --shell 'node scripts/pitch-meddic-gap-check.js'
    stdin: $load-deals.stdout

  # Step 3: Format summary using summarize CLI (Req 39 — zero LLM)
  - id: format-summary
    command: >
      openclaw.invoke --tool summarize --action compress --args-json '{
        "input": $STDIN,
        "format": "decision-forward"
      }'
    stdin: $scan-gaps.stdout

  # Step 4: Deliver to Slack
  - id: deliver
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#pitch-pipeline",
        "text": "📊 DAILY PIPELINE HEALTH\n\n$format-summary.stdout\n\nDeals with gaps: $scan-gaps.json.deals_with_gaps\nDeals clear: $scan-gaps.json.deals_clear"
      }'
    condition: $scan-gaps.json.deals_with_gaps > 0

  # Step 5: Audit log (Req 38)
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/meddic-scan",
        "data": {
          "action": "meddic_scan",
          "total_deals": $scan-gaps.json.total_deals,
          "deals_with_gaps": $scan-gaps.json.deals_with_gaps,
          "alerts": $scan-gaps.json.alerts,
          "authorized_by": "autonomous",
          "timestamp": "{{timestamp}}"
        }
      }'
