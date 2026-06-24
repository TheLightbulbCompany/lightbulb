name: weekly-report
# Weekly support metrics + learning digest (Req 22, 23, 25, 45, 46, 47).
# Runs Monday 8 AM on cron. Fully deterministic, gateway-free.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - `fast-io` is fictional. The original loaders did `fast-io list --prefix vera-csat/`
#    etc.; state.* has NO prefix-list, so the producer workflows (intake-resolve,
#    escalation) append each event to a SINGLE-KEY INDEX ARRAY under `vera/...`.
#    This report reads those arrays, windows them to the last 7 days in a node step,
#    computes metrics deterministically, then COMPACTS the rolling arrays back so they
#    don't grow unbounded (keeps the trailing 35 days).
#  - The two original LLM steps (CSAT failure classification, KB-gap clustering) and
#    all delivery (Slack post, Google Sheet/Doc write) CANNOT run in an embedded
#    pipeline (llm-task / slack / openclaw.invoke all 401). They are written to a
#    `vera/notifications` queue as an intent for the agent loop to pick up and finish
#    (run llm-task, write the gog Sheet/Doc, post Slack). The pipeline itself produces
#    the deterministic metrics + a delivery-ready payload and stops there.
#  - `capability-evolver` is fictional — dropped; its pattern analysis is folded into
#    the same notifications intent for the agent loop (which has model judgment).
#  - Scripts always exit 0 and carry status in JSON; steps branch via `condition:`.
#  - The .js is FLAT in the agent dir (`node vera-metrics-calculator.js`), NOT scripts/.

args:
  period:
    default: "7d"          # reporting window label, passed through to the calculator
  csat_threshold:
    default: "3"           # CSAT <= this counts as a failure (string arg; calculator coerces)

steps:
  # ---- 1-6. Load the rolling event index arrays (single-key, per-agent state) ----
  # Each is an array the producer workflows append to; null when never written.
  - id: load-tickets
    pipeline: state.get vera/ticket-log

  - id: load-escalations
    pipeline: state.get vera/escalation-log

  - id: load-csat
    pipeline: state.get vera/csat-log

  - id: load-kb-gaps
    pipeline: state.get vera/kb-gap-log

  - id: load-kb-conflicts
    pipeline: state.get vera/kb-conflict-log

  - id: load-stale
    pipeline: state.get vera/kb-stale-log

  # ---- 7. Window every array to the last 7 days + assemble the calculator's input ----
  # The original `calculate-metrics` step had NO stdin, so the calculator always saw
  # empty input. Here we build exactly the shape it reads:
  # {tickets, csat_scores, kb_gaps, escalations, period, csat_threshold}.
  # `_event_ts` helper accepts ISO `timestamp`/`created_at`/`queued_at`/`buffered_at`.
  - id: window
    command: >
      node -e 'const cut=Date.now()-7*864e5;
        const ts=o=>{const v=o&&(o.timestamp||o.created_at||o.queued_at||o.buffered_at||o.date);
          const t=v?Date.parse(v):NaN; return Number.isNaN(t)?Date.now():t;};
        const recent=a=>(Array.isArray(a)?a:[]).filter(o=>ts(o)>=cut);
        const tickets=recent(JSON.parse(process.env.TICKETS||"null"));
        const escalations=recent(JSON.parse(process.env.ESC||"null"));
        const csat=recent(JSON.parse(process.env.CSAT||"null"));
        const gaps=recent(JSON.parse(process.env.GAPS||"null"));
        const conflicts=recent(JSON.parse(process.env.CONFLICTS||"null"));
        const stale=(JSON.parse(process.env.STALE||"null"))||[];
        process.stdout.write(JSON.stringify({
          tickets, escalations, csat_scores:csat, kb_gaps:gaps,
          kb_conflicts:conflicts, stale_articles:Array.isArray(stale)?stale:[],
          period:process.env.PERIOD||"7d",
          csat_threshold:parseInt(process.env.CSAT_THRESH||"3",10),
          ticket_count:tickets.length, csat_count:csat.length, gap_count:gaps.length}));'
    env:
      TICKETS: "$load-tickets.json"
      ESC: "$load-escalations.json"
      CSAT: "$load-csat.json"
      GAPS: "$load-kb-gaps.json"
      CONFLICTS: "$load-kb-conflicts.json"
      STALE: "$load-stale.json"
      PERIOD: "${period}"
      CSAT_THRESH: "${csat_threshold}"

  # ---- 8. Deterministic metrics (Req 45, 47). Zero LLM. Reads the windowed input. ----
  - id: calculate-metrics
    command: node vera-metrics-calculator.js
    stdin: $window.stdout

  # ---- 9. Emit an explicit flag for whether there is anything to cluster/classify ----
  # The original gated LLM steps on `.length` (silently false). Emit counts instead.
  - id: gates
    command: >
      node -e 'const w=JSON.parse(require("fs").readFileSync(0,"utf8"));
        const csatFails=(w.csat_scores||[]).filter(c=>{
          const s=(c&&typeof c.score==="number")?c.score:null;
          return s!==null && s<=(w.csat_threshold||3);});
        process.stdout.write(JSON.stringify({
          gap_count:(w.kb_gaps||[]).length,
          csat_fail_count:csatFails.length,
          csat_failures:csatFails,
          kb_gaps:w.kb_gaps||[]}));'
    stdin: $window.stdout

  # ---- 10. Assemble the agent-loop intent payload (LLM + delivery work) ----
  # Carries the deterministic metrics + the raw inputs the agent loop needs to:
  #   - cluster KB gaps (llm-task, Req 22)            [only when gap_count > 0]
  #   - classify CSAT failures (llm-task, Req 46)     [only when csat_fail_count > 0]
  #   - run pattern analysis (model judgment; was the fictional capability-evolver)
  #   - write the Google Sheet + Doc (gog) and post #vera-digest Slack
  - id: build-notification
    command: >
      node -e 'const m=JSON.parse(process.env.METRICS||"{}");
        const g=JSON.parse(process.env.GATES||"{}");
        const date=new Date().toISOString().slice(0,10);
        process.stdout.write(JSON.stringify({
          kind:"weekly_report",
          period:process.env.PERIOD||"7d",
          date,
          metrics:m,
          needs_gap_clustering:(g.gap_count||0)>0,
          needs_csat_classification:(g.csat_fail_count||0)>0,
          kb_gaps:g.kb_gaps||[],
          csat_failures:g.csat_failures||[],
          delivery:{
            slack_channel:"#vera-digest",
            sheet:"Vera Weekly Reports",
            doc:"Vera Weekly Report — "+date,
            doc_folder:"Vera Reports"},
          created_at:new Date().toISOString()}));'
    env:
      METRICS: "$calculate-metrics.stdout"
      GATES: "$gates.stdout"
      PERIOD: "${period}"

  # ---- 11. Load + append to the notifications queue (single-key index array) ----
  - id: notify-load
    pipeline: state.get vera/notifications

  - id: notify-next
    command: >
      node -e 'const q=JSON.parse(process.env.QUEUE||"null")||[];
        const item=JSON.parse(require("fs").readFileSync(0,"utf8"));
        q.push(item);
        process.stdout.write(JSON.stringify(q));'
    env:
      QUEUE: "$notify-load.json"
    stdin: $build-notification.stdout

  - id: notify
    pipeline: state.set vera/notifications
    stdin: $notify-next.stdout

  # ---- 12. Append this run to the audit log index array (was fast-io audit/ write) ----
  - id: audit-load
    pipeline: state.get vera/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.LOG||"null")||[];
        const m=JSON.parse(process.env.METRICS||"{}");
        const g=JSON.parse(process.env.GATES||"{}");
        log.push({action:"weekly_report",
          period:process.env.PERIOD||"7d",
          total_tickets:m.total_tickets||0,
          gap_count:g.gap_count||0,
          csat_fail_count:g.csat_fail_count||0,
          authorized_by:"autonomous",
          timestamp:new Date().toISOString()});
        process.stdout.write(JSON.stringify(log.slice(-500)));'
    env:
      LOG: "$audit-load.json"
      METRICS: "$calculate-metrics.stdout"
      GATES: "$gates.stdout"
      PERIOD: "${period}"

  - id: audit
    pipeline: state.set vera/audit-log
    stdin: $audit-next.stdout

  # ---- 13. Compact the rolling event arrays to a trailing 35-day window ----
  # Keeps the index arrays bounded so each weekly run stays cheap. Re-windows the
  # SAME load steps (loaded before any state.set above, so values are unchanged).
  - id: compact-tickets
    command: >
      node -e 'const cut=Date.now()-35*864e5;
        const ts=o=>{const v=o&&(o.timestamp||o.created_at||o.queued_at||o.buffered_at||o.date);
          const t=v?Date.parse(v):NaN; return Number.isNaN(t)?Date.now():t;};
        const a=(JSON.parse(process.env.IN||"null")||[]).filter(o=>ts(o)>=cut);
        process.stdout.write(JSON.stringify(a));'
    env:
      IN: "$load-tickets.json"

  - id: save-tickets
    pipeline: state.set vera/ticket-log
    stdin: $compact-tickets.stdout

  - id: compact-escalations
    command: >
      node -e 'const cut=Date.now()-35*864e5;
        const ts=o=>{const v=o&&(o.timestamp||o.created_at||o.queued_at||o.buffered_at||o.date);
          const t=v?Date.parse(v):NaN; return Number.isNaN(t)?Date.now():t;};
        const a=(JSON.parse(process.env.IN||"null")||[]).filter(o=>ts(o)>=cut);
        process.stdout.write(JSON.stringify(a));'
    env:
      IN: "$load-escalations.json"

  - id: save-escalations
    pipeline: state.set vera/escalation-log
    stdin: $compact-escalations.stdout

  - id: compact-csat
    command: >
      node -e 'const cut=Date.now()-35*864e5;
        const ts=o=>{const v=o&&(o.timestamp||o.created_at||o.queued_at||o.buffered_at||o.date);
          const t=v?Date.parse(v):NaN; return Number.isNaN(t)?Date.now():t;};
        const a=(JSON.parse(process.env.IN||"null")||[]).filter(o=>ts(o)>=cut);
        process.stdout.write(JSON.stringify(a));'
    env:
      IN: "$load-csat.json"

  - id: save-csat
    pipeline: state.set vera/csat-log
    stdin: $compact-csat.stdout

  - id: compact-gaps
    command: >
      node -e 'const cut=Date.now()-35*864e5;
        const ts=o=>{const v=o&&(o.timestamp||o.created_at||o.queued_at||o.buffered_at||o.date);
          const t=v?Date.parse(v):NaN; return Number.isNaN(t)?Date.now():t;};
        const a=(JSON.parse(process.env.IN||"null")||[]).filter(o=>ts(o)>=cut);
        process.stdout.write(JSON.stringify(a));'
    env:
      IN: "$load-kb-gaps.json"

  - id: save-gaps
    pipeline: state.set vera/kb-gap-log
    stdin: $compact-gaps.stdout
