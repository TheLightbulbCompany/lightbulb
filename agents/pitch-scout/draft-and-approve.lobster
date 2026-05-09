name: draft-and-approve
# Requirements 18, 19, 20, 21, 22, 23, 25, 27, 44, 48
# Triggered after enrichment for qualified prospects.
# LLM calls: 1 (draft generation via llm-task, thinking: medium)

args:
  prospect_domain:
    required: true

steps:
  # Step 1: Load research brief
  - id: load-brief
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "briefs/active/{{prospect_domain}}"
      }'

  # Step 2: Load voice model
  - id: load-voice
    command: >
      openclaw.invoke --tool gog --action drive-read --args-json '{
        "file": "pitch-voice-model.json"
      }'

  # Step 3: Compliance check before drafting (Req 4, 6, 7, 41)
  - id: compliance-check
    command: >
      exec --json --shell 'node scripts/pitch-compliance-check.js'
    stdin: $load-brief.stdout

  # Step 4: Generate draft — ONE LLM CALL (Req 18, 19, 20, 21)
  - id: generate-draft
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Write a first-touch outreach email for this prospect. REQUIREMENTS: 1) Open with the SPECIFIC signal that triggered this prospect — not a generic opener. Reference the actual event, post, or data point. 2) Map the value proposition to THIS prospects specific situation from the research brief — not a category-level pain statement. 3) Match the voice model provided. 4) Adapt format to the channel specified. This is a DRAFT for human review, not a finished product.",
        "thinking": "medium",
        "input": {
          "brief": $load-brief.json,
          "voice_model": $load-voice.json,
          "channel": "email"
        },
        "schema": {
          "type": "object",
          "properties": {
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "channel": {"type": "string"},
            "signal_reference": {"type": "string"},
            "prospect_domain": {"type": "string"}
          },
          "required": ["subject", "body", "channel", "signal_reference", "prospect_domain"]
        }
      }'
    condition: $compliance-check.json.pass

  # Step 5: Content scan — competitor, pricing, commitment, signal grounding (Req 19, 25)
  - id: content-scan
    command: >
      exec --json --shell 'node scripts/pitch-content-scanner.js'
    stdin: $generate-draft.stdout

  # Step 6: Present for approval — APPROVAL GATE (Req 23, 27, 48)
  - id: present-for-approval
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#pitch-approvals",
        "text": "📋 DRAFT FOR REVIEW — edit or approve to send under your name.\n\n**Signal:** $load-brief.json.brief.trigger_signal.context\n**ICP Score:** $load-brief.json.icp_score\n**Confidence:** $load-brief.json.enrichment_confidence\n\n**Subject:** $generate-draft.json.subject\n**Body:** $generate-draft.json.body\n\n**Research Brief:** [View full brief]\n\n⚠️ Content flags: $content-scan.json.flags"
      }'
    approval: required
    condition: $content-scan.json.action != "regenerate"

  # Step 7: Send if approved via connected outbound platform (Req 5)
  # Click-to-connect: Instantly, SmartLead, Mailshake, Lemlist
  - id: load-outbound-config
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "pitch-config/connected-outbound"}'
    condition: $present-for-approval.approved

  - id: no-outbound-notice
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#pitch-approvals",
        "text": "To send outreach, connect your email platform in settings. Supported: Instantly, SmartLead, Mailshake, Lemlist."
      }'
    condition: $present-for-approval.approved && $load-outbound-config.json.platform == null

  - id: send-instantly
    command: >
      exec --json --shell 'curl -s -X POST "https://api.instantly.ai/api/v1/lead/send" \
        -H "Content-Type: application/json" \
        -d "{\"api_key\":\"$INSTANTLY_API_KEY\",\"email\":\"$load-brief.json.brief.prospect_email\",\"subject\":\"$generate-draft.json.subject\",\"body\":\"$generate-draft.json.body\",\"campaign_id\":\"seq_{{prospect_domain}}\"}"'
    condition: $present-for-approval.approved && $load-outbound-config.json.platform == "instantly"

  - id: send-smartlead
    command: >
      exec --json --shell 'curl -s -X POST "https://server.smartlead.ai/api/v1/leads/send" \
        -H "Authorization: Bearer $SMARTLEAD_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"$load-brief.json.brief.prospect_email\",\"subject\":\"$generate-draft.json.subject\",\"body\":\"$generate-draft.json.body\",\"campaign_id\":\"seq_{{prospect_domain}}\"}"'
    condition: $present-for-approval.approved && $load-outbound-config.json.platform == "smartlead"

  - id: send-mailshake
    command: >
      exec --json --shell 'curl -s -X POST "https://api.mailshake.com/2017-04-01/leads/send" \
        -H "Authorization: Basic $MAILSHAKE_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"emailAddress\":\"$load-brief.json.brief.prospect_email\",\"subject\":\"$generate-draft.json.subject\",\"body\":\"$generate-draft.json.body\",\"campaignID\":\"seq_{{prospect_domain}}\"}"'
    condition: $present-for-approval.approved && $load-outbound-config.json.platform == "mailshake"

  - id: send-lemlist
    command: >
      exec --json --shell 'curl -s -X POST "https://api.lemlist.com/api/leads" \
        -H "Authorization: Bearer $LEMLIST_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"$load-brief.json.brief.prospect_email\",\"subject\":\"$generate-draft.json.subject\",\"body\":\"$generate-draft.json.body\",\"campaignId\":\"seq_{{prospect_domain}}\"}"'
    condition: $present-for-approval.approved && $load-outbound-config.json.platform == "lemlist"

  # Step 8: Initialize sequence state in fast-io
  - id: init-sequence
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "sequences/active/{{prospect_domain}}",
        "data": {
          "prospect_domain": "{{prospect_domain}}",
          "sequence_id": "seq_{{prospect_domain}}",
          "touch_count": 1,
          "last_touch_date": "{{timestamp}}",
          "channel": "email",
          "prospect": $load-brief.json.brief,
          "paused": false,
          "approved_by": "rep",
          "first_touch_date": "{{timestamp}}"
        }
      }'
    condition: $present-for-approval.approved

  # Step 9: CRM sync — log the send (Req 17)
  # Click-to-connect: HubSpot, Salesforce, Attio, Pipedrive
  - id: load-crm-config
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "pitch-config/connected-crm"}'
    condition: $present-for-approval.approved

  - id: crm-sync-hubspot
    command: >
      exec --json --shell 'curl -s -X POST "https://api.hubapi.com/crm/v3/objects/engagements" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"properties\":{\"hs_engagement_type\":\"EMAIL\",\"hs_email_subject\":\"$generate-draft.json.subject\",\"hs_timestamp\":\"{{timestamp}}\"},\"associations\":[{\"to\":{\"id\":\"{{prospect_domain}}\"},\"types\":[{\"associationCategory\":\"HUBSPOT_DEFINED\",\"associationTypeId\":198}]}]}"'
    condition: $present-for-approval.approved && $load-crm-config.json.platform == "hubspot"

  - id: crm-sync-salesforce
    command: >
      exec --json --shell 'curl -s -X POST "$SALESFORCE_INSTANCE_URL/services/data/v59.0/sobjects/Task" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"Subject\":\"$generate-draft.json.subject\",\"ActivityDate\":\"{{timestamp}}\",\"Type\":\"Email\",\"WhoId\":\"{{prospect_domain}}\"}"'
    condition: $present-for-approval.approved && $load-crm-config.json.platform == "salesforce"

  - id: crm-sync-attio
    command: >
      exec --json --shell 'curl -s -X POST "https://api.attio.com/v2/objects/companies/records/{{prospect_domain}}/entries" \
        -H "Authorization: Bearer $ATTIO_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"type\":\"email_sent\",\"subject\":\"$generate-draft.json.subject\",\"date\":\"{{timestamp}}\"}"'
    condition: $present-for-approval.approved && $load-crm-config.json.platform == "attio"

  - id: crm-sync-pipedrive
    command: >
      exec --json --shell 'curl -s -X POST "https://api.pipedrive.com/v1/activities?api_token=$PIPEDRIVE_API_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"subject\":\"$generate-draft.json.subject\",\"type\":\"email\",\"done\":1,\"due_date\":\"{{timestamp}}\"}"'
    condition: $present-for-approval.approved && $load-crm-config.json.platform == "pipedrive"

  # Step 10: Audit log (Req 38)
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/first-touch/{{prospect_domain}}",
        "data": {
          "action": "first_touch",
          "domain": "{{prospect_domain}}",
          "approved": true,
          "approved_by": "rep",
          "draft": $generate-draft.json,
          "icp_score": $load-brief.json.icp_score,
          "signal": "$load-brief.json.brief.trigger_signal",
          "content_flags": $content-scan.json.flags,
          "compliance_check": $compliance-check.json,
          "timestamp": "{{timestamp}}"
        }
      }'
