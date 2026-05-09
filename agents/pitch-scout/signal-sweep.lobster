name: signal-sweep
# Requirements 8, 9, 10, 11
# Runs every 4 hours on cron. Scans all signal sources, scores, filters, persists, notifies.
# LLM calls: 1 (signal strength scoring via llm-task, thinking: off)

steps:
  # Step 1: Activation gate
  - id: activation-check
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key":"pitch-config/activation"}'
      | exec --json --shell 'node scripts/pitch-activation-check.js'
    approval: null

  # Step 2: Perplexity API — company news, funding, product launches, regulatory (deterministic)
  - id: perplexity-signals
    command: >
      exec --json --shell 'curl -sS https://api.perplexity.ai/chat/completions \
        -H "Authorization: Bearer $PERPLEXITY_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"sonar\",\"messages\":[{\"role\":\"user\",\"content\":\"{{icp_keywords}} funding OR hiring OR expansion OR acquisition OR product launch. Return structured results with company name, signal type, date, and source URL.\"}]}" \
        | node -e "
          const raw = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
          const content = raw.choices?.[0]?.message?.content || \"\";
          const citations = raw.citations || [];
          process.stdout.write(JSON.stringify({content, citations, total: citations.length, source: \"perplexity\"}));
        "'
    condition: $activation-check.json.pass

  # Step 3: Social intelligence — LinkedIn, Reddit, Twitter
  - id: social-signals
    command: >
      openclaw.invoke --tool social-intelligence --action scan --args-json '{
        "keywords": "{{icp_keywords}}",
        "sources": ["linkedin", "reddit", "twitter"],
        "since": "4h"
      }'

  # Step 4: CRM re-engagement — closed-lost accounts with recent activity (Req 9)
  # Click-to-connect: reads from whichever CRM the user has connected
  - id: load-crm-config
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "pitch-config/connected-crm"}'

  - id: crm-reengagement-hubspot
    command: >
      exec --json --shell 'curl -s "https://api.hubapi.com/crm/v3/objects/contacts/search" \
        -H "Authorization: Bearer $HUBSPOT_ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"filterGroups\":[{\"filters\":[{\"propertyName\":\"hs_lead_status\",\"operator\":\"EQ\",\"value\":\"CLOSED_LOST\"},{\"propertyName\":\"notes_last_updated\",\"operator\":\"GTE\",\"value\":\"{{thirty_days_ago}}\"}]}],\"properties\":[\"domain\",\"notes_last_updated\",\"dealstage\"]}"'
    condition: $load-crm-config.json.platform == "hubspot"

  - id: crm-reengagement-salesforce
    command: >
      exec --json --shell 'curl -s "$SALESFORCE_INSTANCE_URL/services/data/v59.0/query?q=SELECT+Domain__c,LastActivityDate,StageName+FROM+Opportunity+WHERE+StageName='\''Closed+Lost'\''+AND+LastActivityDate>=LAST_N_DAYS:30" \
        -H "Authorization: Bearer $SALESFORCE_ACCESS_TOKEN"'
    condition: $load-crm-config.json.platform == "salesforce"

  - id: crm-reengagement-attio
    command: >
      exec --json --shell 'curl -s "https://api.attio.com/v2/objects/companies/records/query" \
        -H "Authorization: Bearer $ATTIO_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"filter\":{\"status\":\"closed_lost\",\"activity_within\":\"30d\"},\"select\":[\"domain\",\"last_activity_date\",\"last_activity_type\",\"deal_history\"]}"'
    condition: $load-crm-config.json.platform == "attio"

  - id: crm-reengagement-pipedrive
    command: >
      exec --json --shell 'curl -s "https://api.pipedrive.com/v1/deals?status=lost&sort=update_time+DESC&api_token=$PIPEDRIVE_API_TOKEN" \
        | node -e "
          const raw = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
          const deals = (raw.data || []).filter(d => {
            const updated = new Date(d.update_time);
            const cutoff = new Date(Date.now() - 30*24*60*60*1000);
            return updated >= cutoff;
          });
          process.stdout.write(JSON.stringify(deals));
        "'
    condition: $load-crm-config.json.platform == "pipedrive"

  # Normalize CRM re-engagement data from whichever provider responded
  - id: crm-reengagement
    command: >
      exec --json --shell 'node -e "
        const input = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\"));
        const hs = input.hubspot; const sf = input.salesforce; const at = input.attio; const pd = input.pipedrive;
        const data = hs || sf || at || pd || [];
        process.stdout.write(JSON.stringify(Array.isArray(data) ? data : data.results || []));
      "'
    stdin: '{"hubspot": $crm-reengagement-hubspot.json, "salesforce": $crm-reengagement-salesforce.json, "attio": $crm-reengagement-attio.json, "pipedrive": $crm-reengagement-pipedrive.json}'

  # Step 5: Apollo intent data (Req 9)
  - id: apollo-intent
    command: >
      exec --json --shell 'curl -s "https://api.apollo.io/v1/intent_signals?keywords={{icp_keywords}}&limit=20" -H "X-Api-Key: $APOLLO_API_KEY"'

  # Step 6: (Visitor intelligence removed per audit)

  # Step 7: Merge all signal sources
  - id: merge-signals
    command: >
      exec --json --shell 'node -e "
        const perplexity = JSON.parse(process.argv[1] || \"[]\");
        const social = JSON.parse(process.argv[2] || \"[]\");
        const crm = JSON.parse(process.argv[3] || \"[]\");
        const apollo = JSON.parse(process.argv[4] || \"[]\");
        const merged = [
          ...((Array.isArray(perplexity) ? perplexity : [perplexity]).filter(Boolean)).map(s => ({...s, source_type: \"news\"})),
          ...social.map(s => ({...s, source_type: \"social\"})),
          ...crm.map(s => ({...s, source_type: \"crm_reengagement\", signal_type: \"reengagement\"})),
          ...apollo.map(s => ({...s, source_type: \"intent_data\"}))
        ];
        process.stdout.write(JSON.stringify({signals: merged, count: merged.length}));
      " $PERPLEXITY $SOCIAL $CRM $APOLLO'

  # Step 8: Deterministic scoring — ICP fit, recency, relationship (Req 10)
  - id: deterministic-score
    command: >
      exec --json --shell 'node scripts/pitch-signal-scorer.js'
    stdin: $merge-signals.stdout

  # Step 9: LLM signal strength scoring — the one LLM call (Req 10)
  - id: llm-strength-score
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Score the buying intent strength of each signal 0-100. A prospect posting about a specific pain your product solves scores high. A generic company news post scores low. Return the array with strength_score added to each.",
        "thinking": "off",
        "input": $STDIN,
        "schema": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "company": {"type": "string"},
              "signal_type": {"type": "string"},
              "strength_score": {"type": "number"},
              "strength_rationale": {"type": "string"}
            },
            "required": ["company", "strength_score"]
          }
        }
      }'
    stdin: $deterministic-score.stdout

  # Step 10: Final composite scoring and threshold filter
  - id: final-filter
    command: >
      exec --json --shell 'node -e "
        const data = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\", \"utf8\"));
        const threshold = 60;
        const qualified = (data.qualified || data).filter(s => s.composite_score >= threshold);
        process.stdout.write(JSON.stringify({qualified, count: qualified.length, threshold}));
      "'
    stdin: $llm-strength-score.stdout

  # Step 11: Persist to fast-io (Req 38)
  - id: persist-signals
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "signals/{{date}}",
        "data": $STDIN
      }'
    stdin: $final-filter.stdout

  # Step 12: Notify via Slack
  - id: notify
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#pitch-signals",
        "text": "{{count}} new qualified signals detected. Review queue ready."
      }'
    stdin: $persist-signals.stdout

  # Step 13: Audit log (Req 38)
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/signal-sweep",
        "data": {
          "action": "signal_sweep",
          "signals_scanned": $merge-signals.json.count,
          "signals_qualified": $final-filter.json.count,
          "sources": ["perplexity", "social-intelligence", "crm", "apollo"],
          "authorized_by": "autonomous",
          "timestamp": "{{timestamp}}"
        }
      }'
