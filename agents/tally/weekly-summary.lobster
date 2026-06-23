name: weekly-summary
# Monday financial briefing with proactive intelligence (Reqs 29, 38).
# Runs Monday ~8 AM on cron. Fully deterministic (0 LLM in the pipeline).
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - Durable state via state.get/set on the per-owner EFS mount (LOBSTER_STATE_DIR).
#    fast-io is fictional. Each former fast-io key is now a single state key
#    namespaced under `tally/`. Config the user sets (financial-data, corrections,
#    thresholds, benchmarks, vendor-map) is read-only consumer state.
#  - Perplexity is reached gateway-free via real `curl` (PERPLEXITY_API_KEY in env);
#    its JSON is normalized by an inline node step.
#  - The .js scripts are FLAT in the agent dir (node tally-*.js — NOT scripts/...).
#    They read stdin JSON and exit 1 on empty stdin, so every step that calls one
#    FEEDS it assembled stdin.
#  - LLM narrative (capability-evolver judgment) + Slack/Sheets delivery cannot run
#    in an embedded pipeline (401). The briefing payload + the vendor-correction
#    analysis request are written to `tally/notifications` for the agent loop to
#    deliver (Slack #tally-digest) and to reason over. Sheets write is queued the
#    same way (gog sheets-write is a gateway tool here).
#  - Scripts carry status in JSON; steps branch via `condition:`. No `.length` gates
#    — producers emit explicit counts.

args:
  period_days:
    required: false
    default: "60"          # accuracy-tracking window
  tax_rate:
    required: false
    default: "0.25"        # effective rate for quarterly estimate

steps:
  # 1. Load current-period financial data (was fast-io tally-state/financial-data).
  - id: financial-data
    pipeline: state.get tally/financial-data

  # 2. Load the metric thresholds + benchmarks the user configured.
  - id: thresholds
    pipeline: state.get tally/metric-thresholds

  - id: benchmarks
    pipeline: state.get tally/benchmarks

  # 3. Load correction history (drives accuracy + vendor-map analysis).
  - id: corrections
    pipeline: state.get tally/corrections

  # 4. Assemble the metrics-calculator input. It expects
  #    {revenue,cogs,expenses,cash,stripe,headcount,config}; config carries
  #    metric_thresholds + benchmarks. Empty state -> safe zeros (calculator
  #    handles missing fields, but it process.exit(1)s on EMPTY stdin, so we
  #    always emit a JSON object).
  - id: metrics-input
    command: >
      node -e 'const fd=JSON.parse(process.env.FD||"null")||{};
        const th=JSON.parse(process.env.TH||"null")||{};
        const bm=JSON.parse(process.env.BM||"null")||{};
        fd.config=Object.assign({},fd.config,{metric_thresholds:th,benchmarks:bm});
        process.stdout.write(JSON.stringify(fd));'
    env:
      FD: $financial-data.stdout
      TH: $thresholds.stdout
      BM: $benchmarks.stdout

  # 5. Compute all financial metrics + traces + alerts (deterministic math).
  - id: calculate-metrics
    command: node tally-metrics-calculator.js
    stdin: $metrics-input.stdout

  # 6. 60-day categorization accuracy (was fast-io read + gateway exec).
  - id: accuracy-input
    command: >
      node -e 'const c=JSON.parse(process.env.CORR||"null")||[];
        process.stdout.write(JSON.stringify({mode:"accuracy",
          corrections:c, period_days:Number(process.env.LOBSTER_ARG_PERIOD_DAYS||"60")}));'
    env:
      CORR: $corrections.stdout

  - id: accuracy-check
    command: node tally-categorization-engine.js
    stdin: $accuracy-input.stdout

  # 7. Tax deadline status (deterministic threshold check).
  #    Pull YTD income/deductions from financial-data if present.
  - id: tax-input
    command: >
      node -e 'const fd=JSON.parse(process.env.FD||"null")||{};
        const tax=fd.tax||{};
        process.stdout.write(JSON.stringify({mode:"deadline",
          current_date:new Date().toISOString(),
          tax_rate:Number(process.env.LOBSTER_ARG_TAX_RATE||"0.25"),
          ytd_income:tax.ytd_income||0, ytd_deductions:tax.ytd_deductions||0}));'
    env:
      FD: $financial-data.stdout

  - id: tax-status
    command: node tally-tax-tracker.js
    stdin: $tax-input.stdout

  # 8. Proactive 3+ month trend detection over the metric history (Req 38).
  #    Reads history arrays off the computed metrics input (financial-data carries
  #    *_history); emits explicit count for gating.
  - id: trend-detection
    command: >
      node -e 'const fd=JSON.parse(process.env.FD||"null")||{};
        const m=fd.metrics_history||fd||{};
        const trends=[];
        const gm=m.gross_margin_history;
        if (Array.isArray(gm) && gm.length>=3) { const l=gm.slice(-3);
          if (l[0]>l[1] && l[1]>l[2]) trends.push({metric:"gross_margin",direction:"declining",months:3,values:l}); }
        const br=m.burn_rate_history;
        if (Array.isArray(br) && br.length>=3) { const l=br.slice(-3);
          if (l[0]<l[1] && l[1]<l[2]) trends.push({metric:"burn_rate",direction:"increasing",months:3,values:l}); }
        process.stdout.write(JSON.stringify({trends, count:trends.length,
          first_metric: trends.length>0 ? trends[0].metric : ""}));'
    env:
      FD: $financial-data.stdout

  # 9. Conditionally research the leading trend via Perplexity (real curl).
  #    Gate on an explicit count, never .length. The query is built in a node
  #    step (no shell interpolation of step refs) and passed via env to curl.
  - id: trend-query
    condition: $trend-detection.json.count > 0
    command: >
      node -e 'const m=process.env.METRIC||"";
        process.stdout.write("SaaS company "+m+" benchmark 2025 2026 by funding stage");'
    env:
      METRIC: $trend-detection.json.first_metric

  - id: trend-research
    condition: $trend-detection.json.count > 0
    command: >
      curl -sS https://api.perplexity.ai/chat/completions
      -H "Authorization: Bearer $PERPLEXITY_API_KEY"
      -H "Content-Type: application/json"
      -d "$(node -e 'const q=process.env.Q||"";process.stdout.write(JSON.stringify({model:"sonar",messages:[{role:"user",content:q}]}));')"
      | node -e 'let d="";process.stdin.on("data",c=>d+=c);process.stdin.on("end",()=>{
          let content="",citations=[];
          try{const r=JSON.parse(d); content=(r.choices&&r.choices[0]&&r.choices[0].message&&r.choices[0].message.content)||"";
            citations=r.citations||[];}catch(e){}
          process.stdout.write(JSON.stringify({content,citations,source:"perplexity",ok:!!content}));});'
    env:
      Q: $trend-query.stdout

  # 10. Vendor-correction analysis (was capability-evolver, a gateway 401).
  #     The DETERMINISTIC part — which vendors are consistently overridden — runs
  #     in node here; the LLM judgment ("what the correct mapping should be") is
  #     deferred to the agent loop via the notifications queue (step 14).
  - id: correction-analysis
    command: >
      node -e 'const c=JSON.parse(process.env.CORR||"null")||[];
        const by={};
        for (const x of c){ const v=((x.vendor||"unknown")+"").toLowerCase();
          if(!by[v]) by[v]={correct:0,corrected:0,categories:{}};
          if(x.was_correct) by[v].correct++; else { by[v].corrected++;
            const cat=x.corrected_category||x.correct_category||""; if(cat) by[v].categories[cat]=(by[v].categories[cat]||0)+1; } }
        const offenders=Object.entries(by)
          .filter(([_,v])=>v.corrected>=2)
          .map(([vendor,v])=>{ const top=Object.entries(v.categories).sort((a,b)=>b[1]-a[1])[0];
            return {vendor, corrected:v.corrected, total:v.correct+v.corrected,
              suggested_category: top?top[0]:null, suggestion_support: top?top[1]:0}; })
          .sort((a,b)=>b.corrected-a.corrected).slice(0,10);
        process.stdout.write(JSON.stringify({offenders, count:offenders.length}));'
    env:
      CORR: $corrections.stdout

  # 11. Assemble the deterministic briefing payload (no gateway summarize/LLM).
  #     summarize CLI is a url/file compressor, not a financial-briefing formatter,
  #     so the briefing is built directly from the computed JSON. LLM narrative is
  #     queued for the agent loop.
  - id: briefing
    command: >
      node -e 'const j=k=>JSON.parse(process.env[k]||"null");
        const metrics=j("METRICS")||{}; const acc=j("ACC")||{}; const tax=j("TAX")||{};
        const trends=j("TRENDS")||{}; const research=j("RESEARCH")||{}; const corr=j("CORR")||{};
        const today=new Date().toISOString().slice(0,10);
        const payload={ kind:"weekly_briefing", date:today,
          metrics:metrics.metrics||{}, traces:metrics.traces||{}, alerts:metrics.alerts||[],
          accuracy:acc, tax_deadlines:tax.alerts||[],
          trends:trends.trends||[], trend_research: research.ok?research:null,
          correction_offenders: corr.offenders||[],
          generated_at:new Date().toISOString() };
        process.stdout.write(JSON.stringify(payload));'
    env:
      METRICS: $calculate-metrics.stdout
      ACC: $accuracy-check.stdout
      TAX: $tax-status.stdout
      TRENDS: $trend-detection.stdout
      RESEARCH: $trend-research.stdout
      CORR: $correction-analysis.stdout

  # 12. Persist this week's metrics snapshot to a history index array so future
  #     runs have trend history (single-key array, load -> append -> re-write).
  - id: history-load
    pipeline: state.get tally/weekly-history

  - id: history-next
    command: >
      node -e 'const h=JSON.parse(process.env.HIST||"null")||[];
        const m=JSON.parse(process.env.METRICS||"null")||{};
        h.push({date:new Date().toISOString().slice(0,10), metrics:m.metrics||{}, alerts:(m.alerts||[]).length});
        process.stdout.write(JSON.stringify(h.slice(-52)));'
    env:
      HIST: $history-load.stdout
      METRICS: $calculate-metrics.stdout

  - id: history-save
    pipeline: state.set tally/weekly-history
    stdin: $history-next.stdout

  # 13. Queue the briefing for the agent loop to deliver to Slack #tally-digest
  #     and write to the "Tally Weekly Reports" Google Sheet (both gateway-only
  #     here). Also carries the vendor-map LLM-judgment request. Single-key queue
  #     array: load -> append -> re-write.
  - id: notify-load
    pipeline: state.get tally/notifications

  - id: notify-next
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const briefing=JSON.parse(process.env.BRIEFING||"null")||{};
        const corr=JSON.parse(process.env.CORR||"null")||{};
        n.push({
          kind:"weekly_briefing",
          deliver:{ slack_channel:"#tally-digest", sheet:"Tally Weekly Reports" },
          briefing,
          vendor_map_request: (corr.offenders&&corr.offenders.length>0) ? {
            ask:"Confirm corrected category mapping for consistently-overridden vendors; update tally/vendor-map.",
            offenders:corr.offenders } : null,
          created_at:new Date().toISOString() });
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      BRIEFING: $briefing.stdout
      CORR: $correction-analysis.stdout

  - id: notify
    pipeline: state.set tally/notifications
    stdin: $notify-next.stdout

  # 14. Audit log (was fast-io write). Append to a single-key audit index array.
  - id: audit-load
    pipeline: state.get tally/audit-log

  - id: audit-next
    command: >
      node -e 'const a=JSON.parse(process.env.AUDIT||"null")||[];
        const m=JSON.parse(process.env.METRICS||"null")||{};
        const acc=JSON.parse(process.env.ACC||"null")||{};
        const tr=JSON.parse(process.env.TRENDS||"null")||{};
        a.push({ action:"weekly_summary", at:new Date().toISOString(),
          metrics:m.metrics||{}, accuracy:acc.accuracy||null,
          trends_detected:(tr.count||0)>0, authorized_by:"autonomous" });
        process.stdout.write(JSON.stringify(a.slice(-500)));'
    env:
      AUDIT: $audit-load.stdout
      METRICS: $calculate-metrics.stdout
      ACC: $accuracy-check.stdout
      TRENDS: $trend-detection.stdout

  - id: audit-save
    pipeline: state.set tally/audit-log
    stdin: $audit-next.stdout
