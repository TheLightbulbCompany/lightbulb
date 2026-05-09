name: opt-out-handler
# Requirement 42: Process opt-out requests within 10 business days automatically.
# Triggered by outbound platform unsubscribe webhook (Instantly/SmartLead/Mailshake/Lemlist).
# LLM calls: 0 (fully deterministic)

args:
  prospect_domain:
    required: true
  prospect_email:
    required: true
  source:
    default: "unsubscribe_webhook"

steps:
  # Step 1: Add to opt-out list in fast-io
  - id: add-opt-out
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "opt-out/{{prospect_domain}}",
        "data": {
          "domain": "{{prospect_domain}}",
          "email": "{{prospect_email}}",
          "opt_out_date": "{{timestamp}}",
          "source": "{{source}}",
          "processed": true,
          "processing_date": "{{timestamp}}"
        }
      }'

  # Step 2: Pause any active sequences
  - id: pause-sequence
    command: >
      exec --json --shell 'node scripts/pitch-sequence-pauser.js'
    stdin: '{"prospect_domain": "{{prospect_domain}}", "reason": "opt_out"}'

  # Step 3: Update CRM — mark as opted out
  # Click-to-connect: writes to whichever CRM is connected
  - id: load-crm-config
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "pitch-config/connected-crm"}'

  - id: crm-update-hubspot
    command: >
      exec --json --shell 'curl -s -X PATCH "https://api.hubapi.com/crm/v3/objects/contacts/{{prospect_domain}}" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"properties\":{\"opted_out\":true,\"opt_out_date\":\"{{timestamp}}\",\"opt_out_source\":\"{{source}}\",\"hs_lead_status\":\"DO_NOT_CONTACT\"}}"'
    condition: $load-crm-config.json.platform == "hubspot"

  - id: crm-update-salesforce
    command: >
      exec --json --shell 'curl -s -X PATCH "$SALESFORCE_INSTANCE_URL/services/data/v59.0/sobjects/Contact/{{prospect_domain}}" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"HasOptedOutOfEmail\":true,\"Opt_Out_Date__c\":\"{{timestamp}}\",\"Opt_Out_Source__c\":\"{{source}}\",\"Status\":\"Do Not Contact\"}"'
    condition: $load-crm-config.json.platform == "salesforce"

  - id: crm-update-attio
    command: >
      exec --json --shell 'curl -s -X PATCH "https://api.attio.com/v2/objects/companies/records/{{prospect_domain}}" \
        -H "Authorization: Bearer $ATTIO_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"opted_out\":true,\"opt_out_date\":\"{{timestamp}}\",\"opt_out_source\":\"{{source}}\",\"status\":\"do_not_contact\"}"'
    condition: $load-crm-config.json.platform == "attio"

  - id: crm-update-pipedrive
    command: >
      exec --json --shell 'curl -s -X PUT "https://api.pipedrive.com/v1/persons/{{prospect_domain}}?api_token=$PIPEDRIVE_API_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"label\":\"do_not_contact\",\"opted_out\":true}"'
    condition: $load-crm-config.json.platform == "pipedrive"

  # Step 4: Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/opt-out/{{prospect_domain}}",
        "data": {
          "action": "opt_out_processed",
          "domain": "{{prospect_domain}}",
          "email": "{{prospect_email}}",
          "source": "{{source}}",
          "sequence_paused": true,
          "crm_updated": true,
          "authorized_by": "autonomous",
          "timestamp": "{{timestamp}}"
        }
      }'

  # Step 5: Schedule 10-business-day verification cron
  - id: schedule-verification
    command: >
      openclaw.invoke --tool cron --action add --args-json '{
        "name": "opt-out-verify-{{prospect_domain}}",
        "at": "14d",
        "session": "isolated",
        "message": "Verify opt-out processing complete for {{prospect_domain}}. Check fast-io key opt-out/{{prospect_domain}} and confirm CRM status is do_not_contact.",
        "deleteAfterRun": true,
        "lightContext": true
      }'
