name: visitor-alert
# Requirement 24: Website visitor identification as highest-urgency signal.
# Triggered immediately by Leadfeeder/Clearbit Reveal webhook.
# LLM calls: 0-1 (outreach angle if signal doesn't match deterministic map)

args:
  visitor_domain:
    required: true
  page_visited:
    required: true
  visitor_data:
    default: "{}"

steps:
  # Step 1: Dedup — is this company already in queue or active sequence?
  - id: quick-dedup
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "lead-queue/priority/{{visitor_domain}}"
      }'

  - id: check-existing
    command: >
      exec --json --shell 'node -e "
        const existing = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\", \"utf8\") || \"null\");
        if (existing) {
          process.stdout.write(JSON.stringify({exists: true, action: \"append_visitor_signal\"}));
          process.exit(1);
        }
        process.stdout.write(JSON.stringify({exists: false, action: \"process\"}));
      "'
    stdin: $quick-dedup.stdout

  # Step 2: Build signal data
  - id: build-signal
    command: >
      exec --json --shell 'node -e "
        const pageMap = {
          \"pricing\": \"website_visit_pricing\",
          \"demo\": \"website_visit_demo\",
          \"comparison\": \"website_visit_comparison\",
          \"case-study\": \"website_visit_casestudy\",
          \"contact\": \"website_visit_contact\"
        };
        const page = process.env.PAGE || \"unknown\";
        const signalType = Object.entries(pageMap).find(([k]) => page.includes(k))?.[1] || \"website_visit\";
        process.stdout.write(JSON.stringify({
          type: signalType,
          source: \"visitor_identification\",
          domain: process.env.DOMAIN,
          page_visited: page,
          timestamp: new Date().toISOString(),
          summary: \"Company visited \" + page + \" page — highest urgency signal\"
        }));
      "'
    condition: $check-existing.json.exists == false

  # Step 3: Fast enrichment — Apollo only for speed
  - id: fast-enrich
    command: >
      openclaw.invoke --tool apollo --action enrich --args-json '{
        "domain": "{{visitor_domain}}",
        "find_contacts": true
      }'
    condition: $check-existing.json.exists == false

  # Step 4: Email verification — Apollo built-in
  # Apollo's enrichment response includes verification_status on email fields.
  # fast-enrich (Step 3) already returns verified email from Apollo.

  # Step 5: Compliance check
  - id: compliance
    command: >
      exec --json --shell 'node scripts/scout-compliance-check.js'
    stdin: $fast-enrich.stdout

  # Step 6: ICP score with visitor boost
  - id: score
    command: >
      exec --json --shell 'node scripts/scout-icp-scorer.js'
    stdin: $fast-enrich.stdout

  # Step 7: Build dossier
  - id: dossier
    command: >
      exec --json --shell 'node scripts/scout-dossier-builder.js'

  # Step 8: Volume check
  - id: volume-check
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{"key": "scout-state/today-deposits"}'
      | exec --json --shell 'node scripts/scout-volume-limiter.js'

  # Step 9: Deposit to priority queue
  - id: deposit
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "lead-queue/priority/{{visitor_domain}}",
        "data": $dossier.stdout
      }'
    condition: $volume-check.json.deposited_count > 0

  # Step 10: Immediate priority alert
  - id: urgent-alert
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#scout-alerts",
        "text": "🔥 WEBSITE VISITOR — {{visitor_domain}}\n\nPage: {{page_visited}}\nScore: $score.json.scored[0].score (with visitor boost)\nCompany: $fast-enrich.json.company_name\nContact: $fast-enrich.json.primary_contact\n\nThis lead visited your site and self-selected. Work immediately."
      }'

  # Step 11: Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/visitor/{{visitor_domain}}",
        "data": {
          "action": "visitor_alert",
          "domain": "{{visitor_domain}}",
          "page": "{{page_visited}}",
          "score": $score.json.scored[0].score,
          "deposited": true,
          "authorized_by": "autonomous",
          "timestamp": "{{timestamp}}"
        }
      }'
