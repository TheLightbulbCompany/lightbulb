name: sequence-execute
# Requirements 2, 24, 25, 28, 29, 30, 31, 32
# Runs hourly on cron. Checks for due touches, validates, generates, sends.
# LLM calls: 1 (follow-up draft generation via llm-task, thinking: low)

steps:
  # Step 0: Bounce rate gate (Req 2)
  # Reads analytics from whichever outbound platform the user connected
  - id: load-outbound-config
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "pitch-config/connected-outbound"}'

  - id: bounce-analytics-instantly
    command: >
      exec --json --shell 'curl -s "https://api.instantly.ai/api/v1/analytics/campaign?api_key=$INSTANTLY_API_KEY&metric=bounce_rate"'
    condition: $load-outbound-config.json.platform == "instantly"

  - id: bounce-analytics-smartlead
    command: >
      exec --json --shell 'curl -s "https://server.smartlead.ai/api/v1/analytics?metric=bounce_rate" \
        -H "Authorization: Bearer $SMARTLEAD_API_KEY"'
    condition: $load-outbound-config.json.platform == "smartlead"

  - id: bounce-analytics-mailshake
    command: >
      exec --json --shell 'curl -s "https://api.mailshake.com/2017-04-01/analytics" \
        -H "Authorization: Basic $MAILSHAKE_API_KEY"'
    condition: $load-outbound-config.json.platform == "mailshake"

  - id: bounce-analytics-lemlist
    command: >
      exec --json --shell 'curl -s "https://api.lemlist.com/api/campaigns/analytics" \
        -H "Authorization: Bearer $LEMLIST_API_KEY"'
    condition: $load-outbound-config.json.platform == "lemlist"

  - id: bounce-check
    command: >
      exec --json --shell 'node -e "
        const input = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
        const data = input.instantly || input.smartlead || input.mailshake || input.lemlist || {};
        process.stdout.write(JSON.stringify({analytics: data}));
      "'
    stdin: '{"instantly": $bounce-analytics-instantly.json, "smartlead": $bounce-analytics-smartlead.json, "mailshake": $bounce-analytics-mailshake.json, "lemlist": $bounce-analytics-lemlist.json}'
      | exec --json --shell 'node scripts/pitch-bounce-rate-check.js'

  # Step 1: Load active sequences
  - id: load-sequences
    command: >
      openclaw.invoke --tool fast-io --action list --args-json '{
        "prefix": "sequences/active/"
      }'
    condition: $bounce-check.json.pass

  # Step 2: Check due touches (Req 28, 30, 31)
  - id: check-due
    command: >
      exec --json --shell 'node scripts/pitch-sequence-scheduler.js'
    stdin: $load-sequences.stdout

  # Step 3: Load recent signals for interrupt checking (Req 29)
  - id: load-recent-signals
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "signals/latest"
      }'

  # Step 4: Check CRM for status changes (Req 29)
  # Click-to-connect: reads from whichever CRM the user connected
  - id: load-crm-config
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "pitch-config/connected-crm"}'

  - id: crm-changes-hubspot
    command: >
      exec --json --shell 'curl -s "https://api.hubapi.com/crm/v3/objects/deals/search" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"filterGroups\":[{\"filters\":[{\"propertyName\":\"hs_lastmodifieddate\",\"operator\":\"GTE\",\"value\":\"{{one_hour_ago}}\"}]}],\"properties\":[\"dealstage\",\"hs_lead_status\"]}"'
    condition: $load-crm-config.json.platform == "hubspot"

  - id: crm-changes-salesforce
    command: >
      exec --json --shell 'curl -s "$SALESFORCE_INSTANCE_URL/services/data/v59.0/query?q=SELECT+Id,StageName,Status__c+FROM+Opportunity+WHERE+LastModifiedDate>=LAST_N_HOURS:1" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN"'
    condition: $load-crm-config.json.platform == "salesforce"

  - id: crm-changes-attio
    command: >
      exec --json --shell 'curl -s "https://api.attio.com/v2/objects/companies/records/query" \
        -H "Authorization: Bearer $ATTIO_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"filter\":{\"modified_since\":\"1h\"},\"select\":[\"status\",\"deal_stage\"]}"'
    condition: $load-crm-config.json.platform == "attio"

  - id: crm-changes-pipedrive
    command: >
      exec --json --shell 'curl -s "https://api.pipedrive.com/v1/recents?since_timestamp={{one_hour_ago}}&items=deal&api_token=$PIPEDRIVE_API_TOKEN"'
    condition: $load-crm-config.json.platform == "pipedrive"

  - id: crm-changes
    command: >
      exec --json --shell 'node -e "
        const input = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
        const data = input.hubspot || input.salesforce || input.attio || input.pipedrive || [];
        process.stdout.write(JSON.stringify(Array.isArray(data) ? data : data.results || data.data || []));
      "'
    stdin: '{"hubspot": $crm-changes-hubspot.json, "salesforce": $crm-changes-salesforce.json, "attio": $crm-changes-attio.json, "pipedrive": $crm-changes-pipedrive.json}'

  # Step 5: Check for replies (Req 29)
  # Reads reply data from the connected outbound platform
  - id: check-replies-instantly
    command: >
      exec --json --shell 'curl -s "https://api.instantly.ai/api/v1/leads/replies?api_key=$INSTANTLY_API_KEY&since=1h"'
    condition: $load-outbound-config.json.platform == "instantly"

  - id: check-replies-smartlead
    command: >
      exec --json --shell 'curl -s "https://server.smartlead.ai/api/v1/replies?since=1h" \
        -H "Authorization: Bearer $SMARTLEAD_API_KEY"'
    condition: $load-outbound-config.json.platform == "smartlead"

  - id: check-replies-mailshake
    command: >
      exec --json --shell 'curl -s "https://api.mailshake.com/2017-04-01/leads/replies?since=1h" \
        -H "Authorization: Basic $MAILSHAKE_API_KEY"'
    condition: $load-outbound-config.json.platform == "mailshake"

  - id: check-replies-lemlist
    command: >
      exec --json --shell 'curl -s "https://api.lemlist.com/api/activities?type=emailsReplied&since=1h" \
        -H "Authorization: Bearer $LEMLIST_API_KEY"'
    condition: $load-outbound-config.json.platform == "lemlist"

  - id: check-replies
    command: >
      exec --json --shell 'node -e "
        const input = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
        const data = input.instantly || input.smartlead || input.mailshake || input.lemlist || [];
        process.stdout.write(JSON.stringify(Array.isArray(data) ? data : []));
      "'
    stdin: '{"instantly": $check-replies-instantly.json, "smartlead": $check-replies-smartlead.json, "mailshake": $check-replies-mailshake.json, "lemlist": $check-replies-lemlist.json}'

  # Step 6: Interrupt checker (Req 29)
  - id: interrupt-check
    command: >
      exec --json --shell 'node scripts/pitch-interrupt-checker.js'
    stdin: $check-due.stdout

  # Step 7: Route C-suite touches to approval (Req 24)
  # Split: touches requiring approval go to draft-and-approve pipeline
  # Autonomous touches proceed to step 8
  - id: route-csuite
    command: >
      exec --json --shell 'node -e "
        const data = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\", \"utf8\"));
        const autonomous = data.cleared_touches.filter(t => !t.requires_approval);
        const needsApproval = data.cleared_touches.filter(t => t.requires_approval);
        process.stdout.write(JSON.stringify({autonomous, needs_approval: needsApproval}));
      "'
    stdin: $interrupt-check.stdout
    condition: $interrupt-check.json.has_due_touches

  # Step 8: Generate follow-up drafts — ONE LLM CALL for batch (Req 28, 32)
  - id: generate-followups
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Generate follow-up touches for these prospects. RULES: 1) Do NOT reference previous non-responses — no following up, checking in, circling back, or touching base. 2) Each follow-up must take a NEW angle or value prop. 3) Ground each in the original signal or a new relevant data point.",
        "thinking": "low",
        "input": $STDIN,
        "schema": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "prospect_domain": {"type": "string"},
              "subject": {"type": "string"},
              "body": {"type": "string"},
              "touch_number": {"type": "number"},
              "channel": {"type": "string"}
            },
            "required": ["prospect_domain", "subject", "body", "touch_number"]
          }
        }
      }'
    stdin: $route-csuite.stdout
    condition: $route-csuite.json.autonomous.length > 0

  # Step 9: Content scan all drafts (Req 25, 32)
  - id: content-scan
    command: >
      exec --json --shell 'node scripts/pitch-content-scanner.js'
    stdin: $generate-followups.stdout

  # Step 10: Compliance check all prospects (Req 4, 6, 7, 41)
  - id: compliance-check
    command: >
      exec --json --shell 'node scripts/pitch-compliance-check.js'
    stdin: $content-scan.stdout
    condition: $content-scan.json.action != "regenerate"

  # Step 11: Send cleared touches via connected outbound platform
  - id: no-outbound-notice
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#pitch-approvals",
        "text": "To send outreach, connect your email platform in settings. Supported: Instantly, SmartLead, Mailshake, Lemlist."
      }'
    condition: $compliance-check.json.pass && $load-outbound-config.json.platform == null

  - id: send-touches-instantly
    command: >
      exec --json --shell 'curl -s -X POST "https://api.instantly.ai/api/v1/leads/send-batch" \
        -H "Content-Type: application/json" \
        -d "{\"api_key\":\"$INSTANTLY_API_KEY\",\"touches\":$STDIN}"'
    stdin: $compliance-check.stdout
    condition: $compliance-check.json.pass && $load-outbound-config.json.platform == "instantly"

  - id: send-touches-smartlead
    command: >
      exec --json --shell 'curl -s -X POST "https://server.smartlead.ai/api/v1/leads/send-batch" \
        -H "Authorization: Bearer $SMARTLEAD_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$STDIN"'
    stdin: $compliance-check.stdout
    condition: $compliance-check.json.pass && $load-outbound-config.json.platform == "smartlead"

  - id: send-touches-mailshake
    command: >
      exec --json --shell 'curl -s -X POST "https://api.mailshake.com/2017-04-01/leads/send-batch" \
        -H "Authorization: Basic $MAILSHAKE_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$STDIN"'
    stdin: $compliance-check.stdout
    condition: $compliance-check.json.pass && $load-outbound-config.json.platform == "mailshake"

  - id: send-touches-lemlist
    command: >
      exec --json --shell 'curl -s -X POST "https://api.lemlist.com/api/leads/send-batch" \
        -H "Authorization: Bearer $LEMLIST_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$STDIN"'
    stdin: $compliance-check.stdout
    condition: $compliance-check.json.pass && $load-outbound-config.json.platform == "lemlist"

  # Normalize send results from whichever platform
  - id: send-touches
    command: >
      exec --json --shell 'node -e "
        const input = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
        const data = input.instantly || input.smartlead || input.mailshake || input.lemlist || [];
        process.stdout.write(JSON.stringify(Array.isArray(data) ? data : []));
      "'
    stdin: '{"instantly": $send-touches-instantly.json, "smartlead": $send-touches-smartlead.json, "mailshake": $send-touches-mailshake.json, "lemlist": $send-touches-lemlist.json}'
    condition: $compliance-check.json.pass

  # Step 12: Update sequence state in fast-io
  - id: update-sequences
    command: >
      exec --json --shell 'node -e "
        const touches = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\", \"utf8\"));
        const updates = (touches || []).map(t => ({
          key: \"sequences/active/\" + t.prospect_domain,
          update: {
            touch_count: t.touch_number,
            last_touch_date: new Date().toISOString(),
            paused: t.touch_number >= 5
          }
        }));
        process.stdout.write(JSON.stringify(updates));
      "'
    stdin: $send-touches.stdout

  # Step 13: CRM sync — log all sends (Req 17)
  # Click-to-connect: logs to whichever CRM is connected
  - id: crm-sync-hubspot
    command: >
      exec --json --shell 'curl -s -X POST "https://api.hubapi.com/crm/v3/objects/engagements/batch/create" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"inputs\":$STDIN}"'
    stdin: $send-touches.stdout
    condition: $load-crm-config.json.platform == "hubspot"

  - id: crm-sync-salesforce
    command: >
      exec --json --shell 'curl -s -X POST "$SALESFORCE_INSTANCE_URL/services/data/v59.0/composite/sobjects" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"records\":$STDIN}"'
    stdin: $send-touches.stdout
    condition: $load-crm-config.json.platform == "salesforce"

  - id: crm-sync-attio
    command: >
      exec --json --shell 'curl -s -X POST "https://api.attio.com/v2/objects/companies/records/batch/entries" \
        -H "Authorization: Bearer $ATTIO_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"entries\":$STDIN}"'
    stdin: $send-touches.stdout
    condition: $load-crm-config.json.platform == "attio"

  - id: crm-sync-pipedrive
    command: >
      exec --json --shell 'curl -s -X POST "https://api.pipedrive.com/v1/activities?api_token=$PIPEDRIVE_API_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$STDIN"'
    stdin: $send-touches.stdout
    condition: $load-crm-config.json.platform == "pipedrive"

  # Step 14: Audit log (Req 38)
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/sequence-execute",
        "data": {
          "action": "sequence_execute",
          "touches_sent": $send-touches.json.length,
          "touches_interrupted": $interrupt-check.json.interrupted_count,
          "touches_csuite_routed": $route-csuite.json.needs_approval.length,
          "bounce_rate": $bounce-check.json.bounce_rate,
          "authorized_by": "autonomous",
          "timestamp": "{{timestamp}}"
        }
      }'
