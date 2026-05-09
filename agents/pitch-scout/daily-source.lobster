name: daily-source
# Main daily sourcing orchestrator.
# Runs once daily on cron. Processes buffered leads from prior day, then chains
# signal-monitor → enrich-and-score → deposit-lead for each new signal.
# Also resets daily deposit counter.
# LLM calls: 0 at this level (LLM calls happen inside child pipelines)

steps:
  # Step 1: Reset daily deposit counter
  - id: reset-counter
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "scout-state/today-deposits",
        "data": {"count": 0, "date": "{{date}}"}'
      }'

  # Step 2: Process buffered leads from yesterday (Req 64)
  - id: load-buffer
    command: >
      openclaw.invoke --tool fast-io --action list --args-json '{
        "prefix": "lead-buffer/{{yesterday}}/"
      }'

  - id: deposit-buffered
    command: >
      exec --json --shell 'node -e "
        const leads = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\", \"utf8\"));
        const sorted = (leads.keys || []).sort((a, b) => (b.score || 0) - (a.score || 0));
        process.stdout.write(JSON.stringify({leads: sorted, count: sorted.length}));
      "'
    stdin: $load-buffer.stdout
    # Each buffered lead goes through deposit-lead pipeline
    # The volume limiter will cap today's deposits

  # Step 3: Load all active briefs
  - id: load-briefs
    command: >
      openclaw.invoke --tool fast-io --action list --args-json '{
        "prefix": "scout-briefs/active/"
      }'

  # Step 4: Filter to confirmed briefs only (Req 6)
  - id: filter-confirmed
    command: >
      exec --json --shell 'node -e "
        const briefs = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\", \"utf8\"));
        const confirmed = (briefs || []).filter(b => b.status === \"active\" && b.confirmed_by_user === true);
        process.stdout.write(JSON.stringify({briefs: confirmed, count: confirmed.length}));
      "'
    stdin: $load-briefs.stdout

  # Step 5: Run signal-monitor pipeline (chains internally)
  - id: run-signal-monitor
    command: >
      openclaw.invoke --tool lobster --action run --args-json '{
        "pipeline": "pipelines/signal-monitor.lobster",
        "timeoutMs": 120000
      }'
    condition: $filter-confirmed.json.count > 0

  # Step 6: Load today's detected signals
  - id: load-signals
    command: >
      openclaw.invoke --tool fast-io --action read --args-json '{
        "key": "signals/{{date}}"
      }'

  # Step 7: For each qualified signal, run enrich → score → deposit
  # This is orchestrated by the agent or by iterating through signals
  - id: process-signals
    command: >
      exec --json --shell 'node -e "
        const data = JSON.parse(require(\"fs\").readFileSync(\"/dev/stdin\", \"utf8\"));
        const signals = data.qualified || data.signals || [];
        const toProcess = signals.map(s => ({
          domain: s.company_domain || s.domain,
          signal: s,
          brief_id: s.brief_id
        }));
        process.stdout.write(JSON.stringify({to_process: toProcess, count: toProcess.length}));
      "'
    stdin: $load-signals.stdout

  # Step 8: Notify completion
  - id: notify
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#scout-digest",
        "text": "📊 DAILY SOURCING COMPLETE\n\nBriefs active: $filter-confirmed.json.count\nSignals detected: $load-signals.json.count\nLeads to process: $process-signals.json.count\nBuffered from yesterday: $load-buffer.json.count"
      }'

  # Step 9: Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/daily-source",
        "data": {
          "action": "daily_source_run",
          "briefs_active": $filter-confirmed.json.count,
          "signals_detected": $load-signals.json.count,
          "leads_to_process": $process-signals.json.count,
          "buffered_from_yesterday": $load-buffer.json.count,
          "authorized_by": "autonomous",
          "timestamp": "{{timestamp}}"
        }
      }'
