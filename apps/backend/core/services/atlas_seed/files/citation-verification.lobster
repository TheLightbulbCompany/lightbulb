name: citation-verification

# Quarterly cron: verification pass on every external citation in Atlas's own
# AGENTS.md and any Atlas-generated agents' AGENTS.md. On 2 consecutive
# 403/404/changed-content responses, surface a stale-citation warning.
#
# Gateway-free contract (see intake-and-research.lobster header): real programs
# run under bare `command:` (input via stdin / env), durable state via native
# state.get/state.set (citation-tracking is a single object; notifications +
# audit-log are append queues), and Slack/LLM work is queued as an intent on
# atlas/notifications for the agent loop to drain. The enumerate/extract/verify
# steps read the local filesystem + the network directly (genuine fs/https, not
# gateway state). state.* keys here: atlas/citation-tracking, atlas/notifications,
# atlas/audit-log.

steps:
  # Step 1 — Enumerate AGENTS.md files (Atlas's own + every agent dir). fs walk.
  - id: enumerate-agents
    command: node atlas-citation-verifier.js --action=enumerate-agents
    on_error: continue

  # Step 2 — Extract external citations (URLs, arxiv IDs). fs reads each file.
  - id: extract-citations
    command: node atlas-citation-verifier.js --action=extract-citations
    stdin: $enumerate-agents.stdout
    on_error: continue

  # Step 3 — Verify each citation (https; record 200 vs 403/404/error).
  - id: verify-citations
    command: node atlas-citation-verifier.js --action=verify --quarterly
    stdin: $extract-citations.stdout
    on_error: continue

  # Step 4 — Update consecutive-failure tracking (2-strike rule). The prior
  #          tracking loads from state and rides in on TRACKING; the new tracking
  #          comes back on stdout under `.tracking` for the merge+save below.
  - id: tracking-load
    pipeline: state.get atlas/citation-tracking

  - id: update-tracking
    command: node atlas-citation-verifier.js --action=update-tracking
    stdin: $verify-citations.stdout
    env:
      TRACKING: $tracking-load.stdout
    on_error: continue

  # Merge guard — emit the tracking object to persist. If update-tracking crashed
  # (on_error: continue → empty stdout), fall back to the loaded tracking so the
  # shared citation-tracking object is never wiped. NO on_error: continue — this
  # feeds the state.set and must halt rather than save empty.
  - id: tracking-merge
    command: >
      node -e 'const upd=JSON.parse(process.env.UPD||"null");
        const cur=JSON.parse(process.env.CUR||"null")||{citations:{}};
        const out=(upd&&upd.tracking)?upd.tracking:cur;
        process.stdout.write(JSON.stringify(out));'
    env:
      UPD: $update-tracking.stdout
      CUR: $tracking-load.stdout

  - id: tracking-save
    pipeline: state.set atlas/citation-tracking
    stdin: $tracking-merge.stdout

  # Step 5 — Surface stale-citation warnings (>= 2 consecutive failures) by
  #          appending an intent to the notifications queue for the agent loop.
  - id: stale-notify-load
    condition: $update-tracking.json.stale_count > 0
    pipeline: state.get atlas/notifications

  # NO on_error: continue — a throwing append must HALT before its -save so the
  # shared notifications queue is never overwritten with empty stdin.
  - id: stale-notify-next
    condition: $update-tracking.json.stale_count > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const u=JSON.parse(process.env.UPD||"null")||{};
        n.push({kind:"stale_citations", channel:"atlas-status",
          payload:{stale_count:u.stale_count||0, stale_summary:u.stale_summary||"",
            stale_details:u.stale_details||[],
            ask:"Review the affected AGENTS.md files and either re-verify, replace with current source, or drop per H4."},
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $stale-notify-load.stdout
      UPD: $update-tracking.stdout

  - id: stale-notify-save
    condition: $update-tracking.json.stale_count > 0
    pipeline: state.set atlas/notifications
    stdin: $stale-notify-next.stdout

  # Audit log — always appended (even on no-stale). Defaults all inputs so it
  # never references a skipped/crashed producer. Timestamp computed in-node.
  - id: audit-load
    pipeline: state.get atlas/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const en=JSON.parse(process.env.ENUM||"null")||{};
        const v=JSON.parse(process.env.VERIFY||"null")||{};
        const u=JSON.parse(process.env.UPD||"null")||{};
        log.push({action:"citation_verification_pass",
          agents_checked:en.agent_count||0,
          citations_verified:v.total_count||0,
          passed:v.passed_count||0,
          failed_this_run:v.failed_count||0,
          stale_after_2_strikes:u.stale_count||0,
          timestamp:new Date().toISOString()});
        if (log.length>500) log.splice(0,log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      ENUM: $enumerate-agents.stdout
      VERIFY: $verify-citations.stdout
      UPD: $update-tracking.stdout

  - id: audit-save
    pipeline: state.set atlas/audit-log
    stdin: $audit-next.stdout
