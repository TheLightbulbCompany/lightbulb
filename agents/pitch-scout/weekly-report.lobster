name: weekly-report
# Requirements 37, 44, 45, 46: Weekly intelligence report with conversion tracking.
# Runs Monday 8 AM on cron.
# LLM calls: 1 (capability-evolver pattern analysis, thinking: low)

steps:
  # Step 1: Load 7-day signal history
  - id: load-signals
    command: >
      openclaw.invoke --tool fast-io --action list --args-json '{
        "prefix": "audit/",
        "filter": "action:signal_monitor AND timestamp:last_7d"
      }'

  # Step 2: Load 7-day deposit history
  - id: load-deposits
    command: >
      openclaw.invoke --tool fast-io --action list --args-json '{
        "prefix": "audit/",
        "filter": "action:lead_deposit AND timestamp:last_7d"
      }'

  # Step 3: Load 30-day deal outcomes from CRM (Req 37, 44, 45)
  - id: load-outcomes
    command: >
      openclaw.invoke --tool attio-enhanced --action list --args-json '{
        "pipeline": "closed",
        "since": "30d",
        "include": ["domain", "stage", "close_date", "deal_value"]
      }'

  # Step 4: Cross-reference outcomes with original scores and signal types
  - id: conversion-analysis
    command: >
      exec --json --shell 'node -e "
        const deposits = JSON.parse(process.env.DEPOSITS || \"[]\");
        const outcomes = JSON.parse(process.env.OUTCOMES || \"[]\");
        const signalPerformance = {};
        const scorePerformance = {priority: {total: 0, converted: 0}, standard: {total: 0, converted: 0}};
        
        for (const dep of deposits) {
          const domain = dep.domain;
          const signalType = dep.signal_type || \"unknown\";
          const tier = dep.tier || \"standard\";
          
          if (!signalPerformance[signalType]) signalPerformance[signalType] = {total: 0, converted: 0};
          signalPerformance[signalType].total++;
          if (scorePerformance[tier]) scorePerformance[tier].total++;
          
          const converted = outcomes.some(o => o.domain === domain && o.stage === \"closed_won\");
          if (converted) {
            signalPerformance[signalType].converted++;
            if (scorePerformance[tier]) scorePerformance[tier].converted++;
          }
        }
        
        // Calculate rates and flag threshold issues (Req 37)
        const priorityRate = scorePerformance.priority.total > 0 ? scorePerformance.priority.converted / scorePerformance.priority.total : 0;
        const standardRate = scorePerformance.standard.total > 0 ? scorePerformance.standard.converted / scorePerformance.standard.total : 0;
        const thresholdIssue = Math.abs(priorityRate - standardRate) < 0.10 && scorePerformance.priority.total > 5 && scorePerformance.standard.total > 5;
        
        process.stdout.write(JSON.stringify({
          signal_performance: signalPerformance,
          score_performance: scorePerformance,
          threshold_issue: thresholdIssue,
          threshold_recommendation: thresholdIssue ? \"Priority and standard tiers are converting at similar rates. Consider lowering priority threshold.\" : null
        }));
      "'

  # Step 5: Capability-evolver — pattern analysis for signal weight updates (Req 44, 45)
  - id: pattern-analysis
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Analyze signal performance data across verticals. Identify which signal types are over- or under-performing relative to their current weights. Recommend weight adjustments. Also identify any vertical-specific patterns — a signal type that works in tech but fails in healthcare.",
        "thinking": "low",
        "input": $conversion-analysis.stdout,
        "schema": {
          "type": "object",
          "properties": {
            "weight_adjustments": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "signal_type": {"type": "string"},
                  "current_performance": {"type": "string"},
                  "recommended_action": {"type": "string", "enum": ["increase_weight", "decrease_weight", "maintain", "disable"]},
                  "reason": {"type": "string"}
                }
              }
            },
            "vertical_patterns": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"}
          },
          "required": ["weight_adjustments", "summary"]
        }
      }'

  # Step 6: Update signal weights in fast-io
  - id: update-weights
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "scout-config/signal-weights",
        "data": $pattern-analysis.json.weight_adjustments
      }'

  # Step 7: Compile full report using summarize CLI (zero LLM)
  - id: compile-report
    command: >
      openclaw.invoke --tool summarize --action compress --args-json '{
        "input": {
          "signals": $load-signals.json,
          "deposits": $load-deposits.json,
          "conversion_analysis": $conversion-analysis.stdout,
          "pattern_analysis": $pattern-analysis.json,
          "threshold_recommendation": "$conversion-analysis.json.threshold_recommendation"
        },
        "format": "weekly-digest"
      }'

  # Step 8: Write report to Google Doc
  - id: write-report
    command: >
      openclaw.invoke --tool gog --action drive-write --args-json '{
        "file": "Scout Weekly Report — {{date}}",
        "content": $compile-report.stdout,
        "folder": "Scout Reports"
      }'

  # Step 9: Notify via Slack
  - id: notify
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "#scout-digest",
        "text": "📊 WEEKLY SOURCING INTELLIGENCE — {{date}}\n\n$compile-report.stdout\n\nFull report: $write-report.json.url"
      }'

  # Step 10: Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/weekly-report",
        "data": {
          "action": "weekly_report",
          "conversion_analysis": $conversion-analysis.stdout,
          "weight_adjustments": $pattern-analysis.json.weight_adjustments,
          "threshold_issue": $conversion-analysis.json.threshold_issue,
          "authorized_by": "autonomous",
          "timestamp": "{{timestamp}}"
        }
      }'
