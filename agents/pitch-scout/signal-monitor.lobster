name: signal-monitor
# Requirements 19-28: Continuous signal monitoring across all configured sources.
# Runs every 4 hours on cron. Iterates across all active briefs.
# LLM calls: 1-2 (job posting classification for ~30% of postings, review sentiment for ~30% of reviews)

steps:
  # Step 1: Load all active confirmed briefs
  - id: load-briefs
    command: >
      openclaw.invoke --tool fast-io --action list --args-json '{
        "prefix": "scout-briefs/active/"
      }'

  # Step 2: For each brief, get the vertical routing
  - id: route-verticals
    command: >
      exec --json --shell 'node scripts/scout-vertical-router.js'
    stdin: $load-briefs.stdout

  # Step 3: Funding signals — Apollo funding + SEC EDGAR + Perplexity news (Req 20)
  - id: funding-signals
    command: >
      openclaw.invoke --tool apollo --action search --args-json '{
        "query": "{{icp_keywords}}",
        "filter": "recent_funding",
        "limit": 20
      }'

  - id: sec-signals
    command: >
      exec --json --shell 'curl -s "https://efts.sec.gov/LATEST/search-index?q={{icp_keywords}}&dateRange=custom&startdt={{yesterday}}&enddt={{today}}&forms=8-K,S-1,13F"'

  # Step 3b: Perplexity funding news sweep (supplements Apollo + SEC EDGAR)
  - id: perplexity-funding
    command: >
      exec --json --shell 'curl -sS https://api.perplexity.ai/chat/completions \
        -H "Authorization: Bearer $PERPLEXITY_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"sonar\",\"messages\":[{\"role\":\"user\",\"content\":\"Recent funding rounds and investments for companies matching: {{icp_keywords}}. Last 24 hours.\"}]}" \
        | node -e "const d=JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\")); const c=d.choices?.[0]?.message?.content||\"\"; const urls=d.citations||[]; process.stdout.write(JSON.stringify({content:c,citations:urls,total:urls.length,source:\"perplexity\"}));"'

  # Step 4: News and PR signals (Req 26)
  - id: news-signals
    command: >
      exec --json --shell 'curl -sS https://api.perplexity.ai/chat/completions \
        -H "Authorization: Bearer $PERPLEXITY_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"sonar\",\"messages\":[{\"role\":\"user\",\"content\":\"{{icp_keywords}} acquisition OR leadership change OR product launch OR regulatory. Last 24 hours news.\"}]}" \
        | node -e "const d=JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\")); const c=d.choices?.[0]?.message?.content||\"\"; const urls=d.citations||[]; process.stdout.write(JSON.stringify({results:(urls).map((u,i)=>({url:u,content:c,source:\"perplexity\"})),total:urls.length}));"'

  # Step 5: Job posting signals (Req 21)
  - id: job-postings
    command: >
      exec --json --shell 'curl -sS https://api.perplexity.ai/chat/completions \
        -H "Authorization: Bearer $PERPLEXITY_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"sonar\",\"messages\":[{\"role\":\"user\",\"content\":\"{{icp_keywords}} hiring OR job posting OR careers. Recent postings.\"}]}" \
        | node -e "const d=JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\")); const c=d.choices?.[0]?.message?.content||\"\"; const urls=d.citations||[]; process.stdout.write(JSON.stringify({results:(urls).map((u,i)=>({url:u,content:c,source:\"perplexity\"})),total:urls.length}));"'

  - id: classify-jobs-deterministic
    command: >
      exec --json --shell 'node scripts/scout-job-signal-classifier.js'
    stdin: $job-postings.stdout

  - id: classify-jobs-llm
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Classify these job postings as buying intent signals. For each, determine what the company is building and what tools they likely need. Return intent category and suggested outreach angle.",
        "thinking": "low",
        "input": $STDIN,
        "schema": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "company": {"type": "string"},
              "domain": {"type": "string"},
              "intent": {"type": "string"},
              "angle": {"type": "string"},
              "category": {"type": "string"}
            },
            "required": ["company", "intent", "angle"]
          }
        }
      }'
    stdin: $classify-jobs-deterministic.stdout
    condition: $classify-jobs-deterministic.json.needs_llm_count > 0

  # Step 6: Technographic signals (Req 22)
  - id: tech-signals
    command: >
      exec --json --shell 'curl -s "https://api.builtwith.com/v21/api.json?KEY=$BUILTWITH_API_KEY&LOOKUP={{icp_domains}}"'

  # Step 7: Intent data — Bombora (Req 23)
  - id: bombora-intent
    command: >
      exec --json --shell 'curl -s "https://api.bombora.com/v3/surge?topics={{icp_topics}}&limit=20" -H "Authorization: Bearer $BOMBORA_API_KEY"'

  # Step 8: Intent data — 6sense (Req 23)
  - id: sixsense-intent
    command: >
      exec --json --shell 'curl -s "https://api.6sense.com/v3/company/insights?keywords={{icp_keywords}}&limit=20" -H "Authorization: Bearer $SIXSENSE_API_KEY"'

  # Step 9: Reddit and community signals (Req 25)
  - id: community-signals
    command: >
      openclaw.invoke --tool last30days --action research --args-json '{
        "topic": "{{icp_pain_keywords}}",
        "sources": ["reddit", "hackernews"],
        "days": 1
      }'

  # Step 10: Review site monitoring (Req 27)
  - id: review-signals
    command: >
      exec --json --shell 'curl -sS https://api.perplexity.ai/chat/completions \
        -H "Authorization: Bearer $PERPLEXITY_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"sonar\",\"messages\":[{\"role\":\"user\",\"content\":\"{{competitor_names}} review frustrated switching alternative site:g2.com OR site:capterra.com OR site:trustradius.com\"}]}" \
        | node -e "const d=JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\",\"utf8\")); const c=d.choices?.[0]?.message?.content||\"\"; const urls=d.citations||[]; process.stdout.write(JSON.stringify({results:(urls).map((u,i)=>({url:u,content:c,source:\"perplexity\"})),total:urls.length}));"'

  # Step 11: Merge all signals with source tags
  - id: merge-signals
    command: >
      exec --json --shell 'node -e "
        const signals = [];
        const addSignals = (data, type) => {
          if (Array.isArray(data)) data.forEach(s => signals.push({...s, signal_source: type, timestamp: s.timestamp || new Date().toISOString()}));
        };
        try {
          addSignals(JSON.parse(process.env.FUNDING || \"[]\"), \"funding_signal\");
          addSignals(JSON.parse(process.env.SEC || \"[]\"), \"regulatory_filing\");
          addSignals(JSON.parse(process.env.NEWS || \"[]\"), \"news_trigger\");
          addSignals(JSON.parse(process.env.JOBS_DET || \"[]\"), \"job_posting_intent\");
          addSignals(JSON.parse(process.env.JOBS_LLM || \"[]\"), \"job_posting_intent\");
          addSignals(JSON.parse(process.env.TECH || \"[]\"), \"tech_stack_change\");
          addSignals(JSON.parse(process.env.BOMBORA || \"[]\"), \"bombora_intent\");
          addSignals(JSON.parse(process.env.SIXSENSE || \"[]\"), \"6sense_intent\");
          addSignals(JSON.parse(process.env.COMMUNITY || \"[]\"), \"reddit_pain\");
          addSignals(JSON.parse(process.env.REVIEWS || \"[]\"), \"competitor_displacement\");
        } catch(e) {}
        process.stdout.write(JSON.stringify({signals, count: signals.length}));
      "'

  # Step 12: Persist signals to fast-io
  - id: persist-signals
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "signals/{{date}}/{{brief_id}}",
        "data": $STDIN
      }'
    stdin: $merge-signals.stdout

  # Step 13: Source health check (Req 47)
  - id: source-health
    command: >
      exec --json --shell 'node scripts/scout-source-health-check.js'

  - id: health-alert
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#scout-alerts",
        "text": "⚠️ SIGNAL SOURCE ALERT\n\n$source-health.json.alerts"
      }'
    condition: $source-health.json.has_alerts

  # Step 14: Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/signal-monitor",
        "data": {
          "action": "signal_monitor",
          "signals_detected": $merge-signals.json.count,
          "sources_queried": ["apollo", "sec_edgar", "perplexity", "builtwith", "bombora", "6sense", "last30days"],
          "source_health": $source-health.json,
          "authorized_by": "autonomous",
          "timestamp": "{{timestamp}}"
        }
      }'
