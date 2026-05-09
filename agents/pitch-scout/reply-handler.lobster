name: reply-handler
# Requirements 33, 34, 35, 42
# Triggered by outbound platform reply webhook (Instantly/SmartLead/Mailshake/Lemlist).
# LLM calls: 2 (reply draft thinking: medium, MEDDIC extraction thinking: low)

args:
  prospect_domain:
    required: true
  reply_content:
    required: true
  reply_subject:
    default: ""

steps:
  # Step 1: Opt-out detection FIRST (Req 42)
  - id: opt-out-check
    command: >
      exec --json --shell 'node scripts/pitch-opt-out-detector.js'
    stdin: '{"reply_text": "{{reply_content}}", "prospect_domain": "{{prospect_domain}}"}'

  # Step 2: If opt-out detected, process immediately (Req 42)
  - id: process-opt-out
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "opt-out/{{prospect_domain}}",
        "data": {
          "domain": "{{prospect_domain}}",
          "opt_out_date": "{{timestamp}}",
          "source": "reply_keyword_detection",
          "jurisdiction": "pending_lookup"
        }
      }'
    condition: $opt-out-check.json.is_opt_out

  # Step 3: Pause sequence (Req 29, 33)
  - id: pause-sequence
    command: >
      exec --json --shell 'node scripts/pitch-sequence-pauser.js'
    stdin: '{"prospect_domain": "{{prospect_domain}}", "reason": "prospect_reply"}'

  # Step 4: Load conversation history (Req 33)
  - id: load-history
    command: >
      openclaw.invoke --tool fast-io --action list --args-json '{
        "prefix": "audit/*/{{prospect_domain}}"
      }'
    condition: $opt-out-check.json.is_opt_out == false

  # Step 5: Load MEDDIC state (Req 33, 35)
  # Click-to-connect: reads from whichever CRM is connected
  - id: load-crm-config
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "pitch-config/connected-crm"}'
    condition: $opt-out-check.json.is_opt_out == false

  - id: load-meddic-hubspot
    command: >
      exec --json --shell 'curl -s "https://api.hubapi.com/crm/v3/objects/deals/search" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"filterGroups\":[{\"filters\":[{\"propertyName\":\"domain\",\"operator\":\"EQ\",\"value\":\"{{prospect_domain}}\"}]}],\"properties\":[\"meddic_metrics\",\"meddic_economic_buyer\",\"meddic_decision_criteria\",\"meddic_decision_process\",\"meddic_identify_pain\",\"meddic_champion\"]}"'
    condition: $load-crm-config.json.platform == "hubspot" && $opt-out-check.json.is_opt_out == false

  - id: load-meddic-salesforce
    command: >
      exec --json --shell 'curl -s "$SALESFORCE_INSTANCE_URL/services/data/v59.0/query?q=SELECT+MEDDIC_Metrics__c,MEDDIC_Economic_Buyer__c,MEDDIC_Decision_Criteria__c,MEDDIC_Decision_Process__c,MEDDIC_Identify_Pain__c,MEDDIC_Champion__c+FROM+Opportunity+WHERE+Domain__c='\''{{prospect_domain}}'\''" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN"'
    condition: $load-crm-config.json.platform == "salesforce" && $opt-out-check.json.is_opt_out == false

  - id: load-meddic-attio
    command: >
      exec --json --shell 'curl -s "https://api.attio.com/v2/objects/companies/records/query" \
        -H "Authorization: Bearer $ATTIO_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"filter\":{\"domain\":\"{{prospect_domain}}\"},\"select\":[\"meddic\"]}"'
    condition: $load-crm-config.json.platform == "attio" && $opt-out-check.json.is_opt_out == false

  - id: load-meddic-pipedrive
    command: >
      exec --json --shell 'curl -s "https://api.pipedrive.com/v1/deals?domain={{prospect_domain}}&api_token=$PIPEDRIVE_API_TOKEN"'
    condition: $load-crm-config.json.platform == "pipedrive" && $opt-out-check.json.is_opt_out == false

  - id: load-meddic
    command: >
      exec --json --shell 'node -e "
        const input = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
        const data = input.hubspot || input.salesforce || input.attio || input.pipedrive || {};
        const records = data.results || data.records || data.data || [data];
        process.stdout.write(JSON.stringify(records[0] || {}));
      "'
    stdin: '{"hubspot": $load-meddic-hubspot.json, "salesforce": $load-meddic-salesforce.json, "attio": $load-meddic-attio.json, "pipedrive": $load-meddic-pipedrive.json}'
    condition: $opt-out-check.json.is_opt_out == false

  # Step 6: Draft suggested reply — LLM CALL 1 (Req 33)
  - id: draft-reply
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Draft a reply to this prospect. Ground it in conversation history and MEDDIC state. Show what has been confirmed, what is still unknown, and the recommended strategic next step. This is a DRAFT for rep review.",
        "thinking": "medium",
        "input": {
          "prospect_reply": "{{reply_content}}",
          "reply_subject": "{{reply_subject}}",
          "conversation_history": $load-history.json,
          "meddic_state": $load-meddic.json,
          "prospect_domain": "{{prospect_domain}}"
        },
        "schema": {
          "type": "object",
          "properties": {
            "suggested_reply": {"type": "string"},
            "confirmed_fields": {"type": "array", "items": {"type": "string"}},
            "unknown_fields": {"type": "array", "items": {"type": "string"}},
            "recommended_next_step": {"type": "string"},
            "reply_tone": {"type": "string", "enum": ["positive", "skeptical", "question", "soft_decline", "hard_decline", "out_of_office"]}
          },
          "required": ["suggested_reply", "recommended_next_step", "reply_tone"]
        }
      }'
    condition: $opt-out-check.json.is_opt_out == false

  # Step 7: MEDDIC extraction from reply — LLM CALL 2 (Req 35)
  - id: meddic-extract
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Extract MEDDIC signals from this prospect reply. ONLY mark a field as confirmed if the prospect EXPLICITLY stated it. Include the source reference for every update. Do not infer from industry knowledge.",
        "thinking": "low",
        "input": {
          "reply": "{{reply_content}}",
          "current_meddic": $load-meddic.json
        },
        "schema": {
          "type": "object",
          "properties": {
            "has_updates": {"type": "boolean"},
            "updates": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "field": {"type": "string", "enum": ["metrics", "economic_buyer", "decision_criteria", "decision_process", "identify_pain", "champion"]},
                  "value": {"type": "string"},
                  "confirmed": {"type": "boolean"},
                  "source": {"type": "string"}
                },
                "required": ["field", "value", "confirmed", "source"]
              }
            }
          },
          "required": ["has_updates"]
        }
      }'
    condition: $opt-out-check.json.is_opt_out == false

  # Step 8: Update MEDDIC in CRM (Req 35)
  # Writes to whichever CRM is connected
  - id: meddic-update-hubspot
    command: >
      exec --json --shell 'curl -s -X PATCH "https://api.hubapi.com/crm/v3/objects/deals/{{prospect_domain}}" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"properties\":$STDIN}"'
    stdin: $meddic-extract.json.updates
    condition: $meddic-extract.json.has_updates && $load-crm-config.json.platform == "hubspot"

  - id: meddic-update-salesforce
    command: >
      exec --json --shell 'curl -s -X PATCH "$SALESFORCE_INSTANCE_URL/services/data/v59.0/sobjects/Opportunity/{{prospect_domain}}" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$STDIN"'
    stdin: $meddic-extract.json.updates
    condition: $meddic-extract.json.has_updates && $load-crm-config.json.platform == "salesforce"

  - id: meddic-update-attio
    command: >
      exec --json --shell 'curl -s -X PATCH "https://api.attio.com/v2/objects/companies/records/{{prospect_domain}}" \
        -H "Authorization: Bearer $ATTIO_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"meddic\":$STDIN}"'
    stdin: $meddic-extract.json.updates
    condition: $meddic-extract.json.has_updates && $load-crm-config.json.platform == "attio"

  - id: meddic-update-pipedrive
    command: >
      exec --json --shell 'curl -s -X PUT "https://api.pipedrive.com/v1/deals/{{prospect_domain}}?api_token=$PIPEDRIVE_API_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$STDIN"'
    stdin: $meddic-extract.json.updates
    condition: $meddic-extract.json.has_updates && $load-crm-config.json.platform == "pipedrive"

  # Step 9: Present reply + draft for approval — APPROVAL GATE (Req 33, 34)
  - id: present-for-approval
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#pitch-approvals",
        "text": "💬 PROSPECT REPLIED — {{prospect_domain}}\n\n**Their reply:**\n{{reply_content}}\n\n**Tone:** $draft-reply.json.reply_tone\n**MEDDIC confirmed:** $draft-reply.json.confirmed_fields\n**Still unknown:** $draft-reply.json.unknown_fields\n**Next step:** $draft-reply.json.recommended_next_step\n\n**Suggested reply (DRAFT):**\n$draft-reply.json.suggested_reply"
      }'
    approval: required
    condition: $opt-out-check.json.is_opt_out == false

  # Step 10: Send approved reply via Gmail (Req 5 — replies send from rep's real inbox)
  - id: send-reply
    command: >
      openclaw.invoke --tool gog --action gmail-send --args-json '{
        "to": "{{prospect_email}}",
        "subject": "Re: {{reply_subject}}",
        "body": "$draft-reply.json.suggested_reply"
      }'
    condition: $present-for-approval.approved

  # Step 11: Audit log (Req 38)
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/reply/{{prospect_domain}}",
        "data": {
          "action": "reply_handling",
          "domain": "{{prospect_domain}}",
          "prospect_reply": "{{reply_content}}",
          "reply_tone": "$draft-reply.json.reply_tone",
          "suggested_reply": "$draft-reply.json.suggested_reply",
          "approved": $present-for-approval.approved,
          "meddic_updates": $meddic-extract.json.updates,
          "was_opt_out": $opt-out-check.json.is_opt_out,
          "authorized_by": "rep",
          "timestamp": "{{timestamp}}"
        }
      }'
