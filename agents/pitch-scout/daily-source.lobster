name: daily-source
# Daily sourcing orchestrator. Runs once daily on cron.
# Does the deterministic bookkeeping for a sourcing day: resets the daily deposit
# counter, re-surfaces yesterday's buffered overflow leads (score-sorted) so they
# get re-deposited first, snapshots how many confirmed briefs are active + how many
# signals were last detected, and hands the agent loop a single `daily_source` intent
# telling it to run signal-monitor -> enrich-and-score -> deposit-lead per signal and
# post the digest. Fully deterministic (0 LLM) from the pipeline's side.
#
# Contract notes (see the lobster-authoring skill):
#  - DSL verbs (state.get/set) run under `pipeline:`; real programs under `command:`.
#  - There is NO fast-io and NO child-pipeline invocation from the embedded runner
#    (openclaw.invoke -> 401). Durable state is native state.get/set on the per-owner
#    EFS mount, every list is a SINGLE-KEY index array (state.* has no prefix-list).
#  - Keys match deposit-lead.lobster / weekly-report.lobster:
#      scout/today-deposits   object {count, date}   — reset here, incremented by deposit-lead
#      scout/buffer           array                  — yesterday's overflow leads
#      scout/lead-queue       array                  — Pitch handoff queue (read-only here)
#      scout/briefs           array                  — confirmed ICP briefs (read-only here)
#      scout/signals          array                  — last signal-monitor output (read-only here)
#      scout/notifications    array                  — agent-delivery intent queue (appended here)
#  - signal-monitor + per-lead enrich/score/deposit + the Slack digest are genuine
#    agent-loop / child-pipeline work (need LLM + gateway + child runs, all unreachable
#    here). The pipeline records a `daily_source` intent for the agent to execute them.
#  - Inline node always exits 0 and carries status in JSON; steps branch via condition.
#  - {{date}}/{{yesterday}}/{{timestamp}} are INERT — dates are computed inside node steps.

steps:
  # 1. Reset the daily deposit counter to {count:0, date:<today>}. deposit-lead reads +
  #    increments this same key, so resetting it at the start of the day re-opens the
  #    50/day volume budget. Object shape, not an array.
  - id: reset-counter-build
    command: >
      node -e 'process.stdout.write(JSON.stringify({count:0, date:new Date().toISOString().slice(0,10)}));'

  - id: reset-counter
    pipeline: state.set scout/today-deposits
    stdin: $reset-counter-build.stdout

  # 2. Load yesterday's buffered overflow leads (single-key array, may be null on day 1).
  - id: buffer-load
    pipeline: state.get scout/buffer

  # 3. Sort the buffer by score desc so the highest-value overflow leads re-deposit first.
  #    Emits an explicit count (never gate on .length — silently false in conditions).
  - id: buffer-sorted
    command: >
      node -e 'const b=JSON.parse(process.env.BUFFER||"null")||[];
        const sorted=[...b].sort((x,y)=>(y.score||0)-(x.score||0));
        process.stdout.write(JSON.stringify({leads:sorted, count:sorted.length}));'
    env:
      BUFFER: $buffer-load.stdout

  # 4. Clear the buffer now that we have surfaced it for re-deposit. The agent loop runs
  #    deposit-lead for each surfaced lead today; re-buffering of any that overflow again
  #    is owned by deposit-lead (it appends to scout/buffer at the daily limit).
  - id: buffer-clear-build
    condition: $buffer-sorted.json.count > 0
    command: >
      node -e 'process.stdout.write("[]");'

  - id: buffer-clear
    condition: $buffer-sorted.json.count > 0
    pipeline: state.set scout/buffer
    stdin: $buffer-clear-build.stdout

  # 5. Load active confirmed briefs (single-key index array). Each entry carries
  #    {brief_id, status, confirmed_by_user, vertical, ...}. Read-only here.
  - id: briefs-load
    pipeline: state.get scout/briefs

  # 6. Filter to confirmed-active briefs only (Req 6) — never source against an
  #    unconfirmed or paused brief. Deterministic; emits an explicit count to gate on.
  - id: briefs-confirmed
    command: >
      node -e 'const all=JSON.parse(process.env.BRIEFS||"null")||[];
        const confirmed=all.filter(b=>b && b.status==="active" && b.confirmed_by_user===true);
        process.stdout.write(JSON.stringify({briefs:confirmed, count:confirmed.length,
          brief_ids:confirmed.map(b=>b.brief_id||b.id||"").filter(Boolean)}));'
    env:
      BRIEFS: $briefs-load.stdout

  # 7. Load the most recent signal-monitor output (single-key snapshot, may be null).
  #    Written by signal-monitor's rebuilt persist step (scout/signals); read-only here.
  - id: signals-load
    pipeline: state.get scout/signals

  # 8. Map the latest signals into a deterministic to_process list (domain + signal +
  #    brief_id) so the agent loop can chain enrich-and-score -> deposit-lead per entry.
  #    Prefers the qualified subset; falls back to all signals. Emits an explicit count.
  - id: process-signals
    command: >
      node -e 'const data=JSON.parse(process.env.SIGNALS||"null")||{};
        const signals=(data.qualified||data.signals||[]);
        const toProcess=signals.map(s=>({domain:(s.company_domain||s.domain||"").toLowerCase(),
          signal:s, brief_id:s.brief_id||""})).filter(x=>x.domain);
        process.stdout.write(JSON.stringify({to_process:toProcess, count:toProcess.length}));'
    env:
      SIGNALS: $signals-load.stdout

  # 9. Assemble the deterministic run summary (counts + the buffered/to-process payloads).
  - id: summary
    command: >
      node -e 'const buf=JSON.parse(process.env.BUFFER||"{}");
        const br=JSON.parse(process.env.BRIEFS||"{}");
        const proc=JSON.parse(process.env.PROC||"{}");
        process.stdout.write(JSON.stringify({
          date:new Date().toISOString().slice(0,10),
          briefs_active:br.count||0,
          brief_ids:br.brief_ids||[],
          signals_detected:proc.count||0,
          to_process:proc.to_process||[],
          buffered_from_yesterday:buf.count||0,
          buffered_leads:buf.leads||[]
        }));'
    env:
      BUFFER: $buffer-sorted.stdout
      BRIEFS: $briefs-confirmed.stdout
      PROC: $process-signals.stdout

  # 10. Load the agent-delivery queue so we can append the daily_source intent.
  - id: notify-load
    pipeline: state.get scout/notifications

  # 11. Append a `daily_source` intent for the agent loop. The agent owns everything the
  #     embedded pipeline cannot do: running signal-monitor (only when confirmed briefs
  #     exist), chaining enrich-and-score -> deposit-lead for each buffered + newly
  #     detected lead, and posting the digest to #scout-digest. needs_action drives the
  #     agent; needs_signal_monitor flags whether sourcing should run this cycle.
  - id: notify-next
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const s=JSON.parse(process.env.SUMMARY||"{}");
        n.push({
          kind:"daily_source",
          needs_action:true,
          needs_signal_monitor:(s.briefs_active||0)>0,
          deliver_to:"#scout-digest",
          date:s.date,
          briefs_active:s.briefs_active||0,
          brief_ids:s.brief_ids||[],
          signals_detected:s.signals_detected||0,
          to_process:s.to_process||[],
          buffered_from_yesterday:s.buffered_from_yesterday||0,
          buffered_leads:s.buffered_leads||[],
          digest:"DAILY SOURCING\n\nBriefs active: "+(s.briefs_active||0)+
            "\nSignals detected: "+(s.signals_detected||0)+
            "\nLeads to process: "+((s.to_process||[]).length)+
            "\nBuffered from yesterday: "+(s.buffered_from_yesterday||0),
          created_at:new Date().toISOString()
        });
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      SUMMARY: $summary.stdout

  - id: notify
    pipeline: state.set scout/notifications
    stdin: $notify-next.stdout

  # 12. Persist this run's summary snapshot to durable state (audit / "what ran today").
  - id: run-save
    pipeline: state.set scout/daily-source-latest
    stdin: $summary.stdout
