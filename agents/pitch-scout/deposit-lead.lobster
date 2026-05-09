name: deposit-lead
# Requirements 39-43, 54, 55, 59, 60, 64: Dedup, volume limit, deposit with full dossier.
# Triggered after enrich-and-score completes for each qualified lead.
# LLM calls: 0 (fully deterministic)

args:
  lead_domain:
    required: true
  dossier:
    required: true
  scoring:
    required: true

steps:
  # Step 1: Score threshold check (Req 59)
  - id: threshold-check
    command: >
      exec --json --shell 'node -e "
        const input = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\", \"utf8\"));
        const score = input.scoring.score || 0;
        const tier = input.scoring.tier;
        if (tier === \"archive\") {
          process.stdout.write(JSON.stringify({pass: false, action: \"archive\", score, reason: \"Below threshold. Routed to research archive.\"}));
          process.exit(1);
        }
        process.stdout.write(JSON.stringify({pass: true, score, tier}));
      "'
    stdin: $scoring

  # Step 2: Load connected CRM config
  - id: load-crm-config
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "scout-config/connected-crm"
      }'
    condition: $threshold-check.json.pass

  # Step 2b: CRM dedup — conditional per connected platform (Req 39)
  - id: load-crm-hubspot
    command: >
      exec --json --shell 'curl -s "https://api.hubapi.com/crm/v3/objects/contacts/search" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"filterGroups\":[{\"filters\":[{\"propertyName\":\"domain\",\"operator\":\"EQ\",\"value\":\"{{lead_domain}}\"}]}]}"'
    condition: $load-crm-config.json.platform == "hubspot"

  - id: load-crm-salesforce
    command: >
      exec --json --shell 'curl -s "$SALESFORCE_INSTANCE_URL/services/data/v59.0/query?q=SELECT+Id,Name,Email,StageName+FROM+Contact+WHERE+Domain__c='"'"'{{lead_domain}}'"'"'" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN"'
    condition: $load-crm-config.json.platform == "salesforce"

  - id: load-crm-attio
    command: >
      openclaw.invoke --tool attio-enhanced --action search --args-json '{
        "query": "{{lead_domain}}",
        "include": ["contacts", "deals", "status"]
      }'
    condition: $load-crm-config.json.platform == "attio"

  - id: load-crm-pipedrive
    command: >
      exec --json --shell 'curl -s "https://api.pipedrive.com/v1/persons/search?term={{lead_domain}}&api_token=$PIPEDRIVE_API_TOKEN"'
    condition: $load-crm-config.json.platform == "pipedrive"

  # Step 2c: Normalize CRM dedup results
  - id: load-crm
    command: >
      exec --json --shell 'node -e "
        const h = JSON.parse(process.env.HUBSPOT || \"null\");
        const s = JSON.parse(process.env.SALESFORCE || \"null\");
        const a = JSON.parse(process.env.ATTIO || \"null\");
        const p = JSON.parse(process.env.PIPEDRIVE || \"null\");
        const data = h || s || a || p || {results:[]};
        const results = data.results || data.records || data.data?.items || [];
        process.stdout.write(JSON.stringify({contacts: Array.isArray(results) ? results : [], source: h ? \"hubspot\" : s ? \"salesforce\" : a ? \"attio\" : p ? \"pipedrive\" : \"none\"}));
      "'

  # Step 3: Load Scout queue for dedup (Req 40)
  - id: load-queue
    command: >
      openclaw.invoke --tool fast-io --action list --args-json '{
        "prefix": "lead-queue/"
      }'

  # Step 4: Load Pitch active sequences for dedup (Req 41)
  - id: load-pitch-sequences
    command: >
      openclaw.invoke --tool fast-io --action list --args-json '{
        "prefix": "sequences/active/"
      }'

  # Step 5: Run dedup check
  - id: dedup-check
    command: >
      exec --json --shell 'node scripts/scout-dedup-check.js'

  # Step 6: Handle dedup result
  - id: handle-dedup
    command: >
      exec --json --shell 'node -e "
        const result = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\", \"utf8\"));
        if (result.hard_block) {
          process.stdout.write(JSON.stringify({action: \"drop\", reason: result.duplicates}));
          process.exit(1);
        }
        if (result.action === \"append_signal\") {
          process.stdout.write(JSON.stringify({action: \"append_signal\", target: result.duplicates[0]}));
          process.exit(0);
        }
        if (result.action === \"append_signal_to_sequence\") {
          process.stdout.write(JSON.stringify({action: \"append_to_pitch\", target: result.duplicates[0]}));
          process.exit(0);
        }
        process.stdout.write(JSON.stringify({action: \"deposit\"}));
      "'
    stdin: $dedup-check.stdout

  # Step 7: Volume limit check (Req 54, 64)
  - id: volume-check
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "scout-state/today-deposits"
      }'
      | exec --json --shell 'node scripts/scout-volume-limiter.js'
    condition: $handle-dedup.json.action == "deposit"

  # Step 8: Check if Pitch is installed (Req 55)
  - id: check-pitch
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "scout-config/pitch-installed"
      }'
    condition: $volume-check.json.deposited_count > 0

  # Step 9a: Deposit to Pitch queue (when Pitch installed)
  - id: deposit-to-pitch
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "$scoring.json.queue/{{lead_domain}}",
        "data": $dossier
      }'
    condition: $check-pitch.json.value == true

  # Step 9b: Deposit to CRM directly — conditional per connected platform (Req 55)
  - id: deposit-to-crm-hubspot
    command: >
      exec --json --shell 'curl -s "https://api.hubapi.com/crm/v3/objects/contacts" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"properties\":{\"email\":\"$dossier.json.contact.email\",\"jobtitle\":\"$dossier.json.contact.title\",\"company\":\"$dossier.json.company.name\",\"icp_score\":\"$scoring.json.score\",\"signal_type\":\"$dossier.json.trigger_signal.type\",\"notes\":\"$dossier.json.trigger_signal.summary\"}}"'
    condition: $check-pitch.json.value == false && $load-crm-config.json.platform == "hubspot"

  - id: deposit-to-crm-salesforce
    command: >
      exec --json --shell 'curl -s "$SALESFORCE_INSTANCE_URL/services/data/v59.0/sobjects/Contact" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"Email\":\"$dossier.json.contact.email\",\"Title\":\"$dossier.json.contact.title\",\"Company\":\"$dossier.json.company.name\",\"ICP_Score__c\":\"$scoring.json.score\",\"Signal_Type__c\":\"$dossier.json.trigger_signal.type\",\"Description\":\"$dossier.json.trigger_signal.summary\"}"'
    condition: $check-pitch.json.value == false && $load-crm-config.json.platform == "salesforce"

  - id: deposit-to-crm-attio
    command: >
      openclaw.invoke --tool attio-enhanced --action create-contact --args-json '{
        "domain": "{{lead_domain}}",
        "email": "$dossier.json.contact.email",
        "title": "$dossier.json.contact.title",
        "company": "$dossier.json.company.name",
        "icp_score": "$scoring.json.score",
        "signal_type": "$dossier.json.trigger_signal.type",
        "notes": "$dossier.json.trigger_signal.summary"
      }'
    condition: $check-pitch.json.value == false && $load-crm-config.json.platform == "attio"

  - id: deposit-to-crm-pipedrive
    command: >
      exec --json --shell 'curl -s "https://api.pipedrive.com/v1/persons?api_token=$PIPEDRIVE_API_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"name\":\"$dossier.json.contact.name\",\"email\":[{\"value\":\"$dossier.json.contact.email\",\"primary\":true}],\"org_id\":null,\"icp_score\":\"$scoring.json.score\",\"signal_type\":\"$dossier.json.trigger_signal.type\"}"'
    condition: $check-pitch.json.value == false && $load-crm-config.json.platform == "pipedrive"

  # Step 9b-no-crm: No CRM connected and no Pitch — notify user
  - id: no-crm-notice
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#scout-alerts",
        "text": "Lead {{lead_domain}} scored $scoring.json.score but cannot be deposited — no CRM connected and Pitch not installed. To deposit leads, connect your CRM in settings. Supported: HubSpot, Salesforce, Attio, Pipedrive."
      }'
    condition: $check-pitch.json.value == false && $load-crm-config.json.platform == null

  # Step 9c: Slack notification with full dossier (when Pitch not installed)
  - id: notify-slack
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#scout-leads",
        "text": "🎯 NEW LEAD — {{lead_domain}}\n\nScore: $scoring.json.score ($scoring.json.tier)\nSignal: $dossier.json.trigger_signal.summary\nContact: $dossier.json.contact.title at $dossier.json.company.name\nAngle: $dossier.json.outreach_angle\nUrgency: $dossier.json.urgency"
      }'
    condition: $check-pitch.json.value == false

  # Step 10: Priority alert for urgent leads (score 90+ or visitor)
  - id: urgency-alert
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#scout-alerts",
        "text": "🔥 URGENT LEAD — {{lead_domain}}\nScore: $scoring.json.score\nSignal: $dossier.json.trigger_signal.summary\nThis lead should be worked immediately."
      }'
    condition: $dossier.json.urgency

  # Step 11: Update daily deposit counter
  - id: update-counter
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "scout-state/today-deposits",
        "data": {"count": $volume-check.json.today_total, "date": "{{date}}"}'
      }'

  # Step 12: Buffer overflow leads for next day (Req 64)
  - id: buffer-overflow
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "lead-buffer/{{date}}/{{lead_domain}}",
        "data": $dossier
      }'
    condition: $volume-check.json.at_limit

  # Step 13: Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/deposit/{{lead_domain}}",
        "data": {
          "action": "lead_deposit",
          "domain": "{{lead_domain}}",
          "score": $scoring.json.score,
          "tier": $scoring.json.tier,
          "destination": "$check-pitch.json.value ? \"pitch_queue\" : \"crm_direct\"",
          "dedup_result": $handle-dedup.json.action,
          "urgency": $dossier.json.urgency,
          "dossier_complete": $dossier.json.complete,
          "authorized_by": "autonomous",
          "timestamp": "{{timestamp}}"
        }
      }'
