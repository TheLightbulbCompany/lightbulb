name: intake-and-research

# Steps 1 + 2: Intake (identity lock, stack overlap) + Research (cookbook + failure cases + brief synthesis).
#
# Gateway-free contract (the embedded runner has NO gateway — gateway tool calls
# 401, and shelling out to the gateway exec proxy exits 127):
#  - Real programs run under `command:` (bare `node x.js`), input via `stdin:`,
#    args via the runner-set `LOBSTER_ARG_*` process env (e.g.
#    `$LOBSTER_ARG_BUILD_ID` in a shell, `process.env.LOBSTER_ARG_*` in node).
#  - Durable per-build state via native `state.get`/`state.set`. state.* keys are
#    plain strings with NO interpolation, so each build is namespaced INSIDE the
#    value (a map keyed by build_id), not in the key path. Keys used here:
#    `atlas/intakes`, `atlas/notifications`, `atlas/audit-log`.
#  - LLM / Slack / summarize work has no gateway-free equivalent, so it is queued
#    as an intent on `atlas/notifications` (load-get -> node append -> set) that the
#    agent loop drains and writes results back into state (e.g. the research brief
#    lands at `atlas/research-briefs[build_id]`, which architect-proposal loads).
#  - Hard confirm gates are native `approval:` blocks; downstream steps gate on
#    `$<gate-id>.approved`.
#  - .js tool steps set `on_error: continue` so a script abort never kills the run;
#    downstream node steps default their inputs (`JSON.parse(x||"null")`).

args:
  user_request:
    default: ""
  build_id:
    default: ""

steps:
  # Step 1.1 — Echo the user_request arg so the identity-lock script can read it
  #            on stdin (the runner exposes args as LOBSTER_ARG_* env vars).
  - id: request-input
    command: >
      node -e 'process.stdout.write(process.env.LOBSTER_ARG_USER_REQUEST||"")'

  # Step 1.2 — Identity lock (H3 enforcement, immutable record + SHA256, ATLAS1
  #            regex check). build_id passed as a flag; the request arrives on stdin.
  - id: identity-lock
    command: node atlas-identity-lock.js --build-id="$LOBSTER_ARG_BUILD_ID"
    stdin: $request-input.stdout
    on_error: continue

  # Step 1.3 — Persist the locked intake into the per-build map so architect-proposal
  #            (a separate pipeline) can load it. Load-before-use, set [build_id], write.
  - id: intake-load
    pipeline: state.get atlas/intakes

  - id: intake-next
    command: >
      node -e 'const map=JSON.parse(process.env.INTAKES||"null")||{};
        const id=process.env.LOBSTER_ARG_BUILD_ID||"build_unknown";
        map[id]=JSON.parse(process.env.ILOCK||"null")||{};
        process.stdout.write(JSON.stringify(map));'
    env:
      INTAKES: $intake-load.stdout
      ILOCK: $identity-lock.stdout

  - id: intake-save
    pipeline: state.set atlas/intakes
    stdin: $intake-next.stdout

  # Step 1.4 — Stack overlap (structured-fields-only per B4).
  - id: stack-overlap
    command: node atlas-stack-overlap.js
    stdin: $identity-lock.stdout
    on_error: continue

  # Step 1.5 — Conditional overlap-disambig (only when overlap_score in the 30-70
  #            band). The LLM classification has no gateway-free equivalent, so we
  #            queue an `overlap_disambig` intent for the agent loop to resolve and
  #            surface; the loop writes its recommendation back for the surface gate.
  - id: overlap-notify-load
    condition: $stack-overlap.json.overlap_score >= 30 && $stack-overlap.json.overlap_score <= 70
    pipeline: state.get atlas/notifications

  - id: overlap-notify-next
    on_error: continue
    condition: $stack-overlap.json.overlap_score >= 30 && $stack-overlap.json.overlap_score <= 70
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const so=JSON.parse(process.env.STACK||"null")||{};
        n.push({kind:"overlap_disambig", channel:"atlas-vet",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          payload:{overlap_score:so.overlap_score, conflicts:so.conflicts||[],
            ask:"Classify capability overlap (extend | separate | no_overlap) and surface the recommendation before research."},
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $overlap-notify-load.stdout
      STACK: $stack-overlap.stdout

  - id: overlap-notify-save
    condition: $stack-overlap.json.overlap_score >= 30 && $stack-overlap.json.overlap_score <= 70
    pipeline: state.set atlas/notifications
    stdin: $overlap-notify-next.stdout

  # Step 1.6 — Surface the stack-overlap recommendation and HARD-GATE on the user
  #            before Step 2 research begins. Replaces the old slack-post + approval.
  - id: surface-overlap-gate
    approval:
      required: true
      prompt: "Stack overlap check — recommendation: $stack-overlap.json.recommendation (score: $stack-overlap.json.overlap_score). A recommendation is only produced when the score is ambiguous (30-70); a null recommendation outside that band is expected (clear extend/separate). A null recommendation WITH a 30-70 score means the overlap script errored — check the tool output before approving. Review in #atlas-vet and approve to proceed to research."

  # Step 2.1 — Cookbook read (two sources combined per source-preference table).
  - id: cookbook-read
    condition: $surface-overlap-gate.approved
    command: node atlas-cookbook-reader.js
    stdin: $identity-lock.stdout
    on_error: continue

  # Step 2.2 — Failure-case routing (refuses any unverified-source case per H4).
  - id: failure-case-route
    condition: $surface-overlap-gate.approved
    command: node atlas-failure-case-router.js
    stdin: $identity-lock.stdout
    on_error: continue

  # Step 2.3 + 2.4 — Compress cookbook reads + compile research brief. Both are LLM
  #            work (summarize + llm-task) with no gateway-free equivalent, so we
  #            queue ONE `research_brief` intent carrying everything the agent loop
  #            needs. The loop synthesizes the brief and persists it at
  #            `atlas/research-briefs[build_id]` (architect-proposal loads it).
  #            Load-before-use, append, re-write.
  - id: brief-notify-load
    condition: $surface-overlap-gate.approved
    pipeline: state.get atlas/notifications

  - id: brief-notify-next
    on_error: continue
    condition: $surface-overlap-gate.approved
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        n.push({kind:"research_brief", channel:"atlas-vet",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          payload:{cookbook:JSON.parse(process.env.COOKBOOK||"null"),
            failure_cases:JSON.parse(process.env.FAILURES||"null"),
            intake:JSON.parse(process.env.ILOCK||"null"),
            persist_to:"atlas/research-briefs",
            instructions:"Synthesize cookbook reads + failure cases into a research brief. Cite specific file paths and doc URLs for every claim. Write the brief into atlas/research-briefs keyed by build_id."},
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $brief-notify-load.stdout
      COOKBOOK: $cookbook-read.stdout
      FAILURES: $failure-case-route.stdout
      ILOCK: $identity-lock.stdout

  - id: brief-notify-save
    condition: $surface-overlap-gate.approved
    pipeline: state.set atlas/notifications
    stdin: $brief-notify-next.stdout

  # Audit log — append a deterministic record to the single-key audit array.
  #            Always runs; defaults its inputs so it never references a skipped
  #            producer. Timestamp computed in-node (no template placeholder).
  - id: audit-load
    pipeline: state.get atlas/audit-log

  - id: audit-next
    on_error: continue
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const so=JSON.parse(process.env.STACK||"null")||{};
        log.push({action:"intake_and_research_complete",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          overlap_recommendation:so.recommendation||null,
          research_brief_queued:true,
          timestamp:new Date().toISOString()});
        if (log.length>500) log.splice(0,log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      STACK: $stack-overlap.stdout

  - id: audit-save
    pipeline: state.set atlas/audit-log
    stdin: $audit-next.stdout
