name: vet-walk

# Step 4: Vet loop. One run presents the NEXT un-vetted requirement row (or
# finalizes when none remain), drop-resilient resume from the next un-vetted row.
#
# Gateway-free contract (see intake-and-research.lobster header): the walker runs
# as a bare `node` program reading the requirement matrix + decisions via env
# (extracted from the per-build maps). Per-requirement decisions are NOT captured
# inside a single pipeline run — a native approval can only carry approve/reject,
# but a vet decision is approve/change/drop PLUS trigger/classification edits. So
# each row is surfaced as a `vet_row` intent on atlas/notifications; the agent
# loop collects the user's decision, normalizes it (atlas-requirement-walker.js
# --action=apply-trigger-edits reads the decision on stdin), writes it into
# atlas/decisions[build_id][req_id], and RE-INVOKES this pipeline for the next
# row. That agent-loop re-invocation replaces the old n8n self-trigger (removed).
# state.* keys here: atlas/requirement-matrix, atlas/decisions, atlas/vet-progress,
# atlas/notifications, atlas/audit-log.

args:
  build_id:
    default: ""

steps:
  # Load the per-build requirement matrix + decisions, then extract THIS build's
  # objects so the walker gets a single JSON blob on env (never a .json.field).
  - id: matrix-load
    pipeline: state.get atlas/requirement-matrix

  - id: decisions-load
    pipeline: state.get atlas/decisions

  - id: matrix-of-build
    command: >
      node -e 'const m=JSON.parse(process.env.MAP||"null")||{};
        const id=process.env.LOBSTER_ARG_BUILD_ID||"";
        process.stdout.write(JSON.stringify(m[id]||null));'
    env:
      MAP: $matrix-load.stdout

  - id: decisions-of-build
    command: >
      node -e 'const m=JSON.parse(process.env.MAP||"null")||{};
        const id=process.env.LOBSTER_ARG_BUILD_ID||"";
        process.stdout.write(JSON.stringify(m[id]||{}));'
    env:
      MAP: $decisions-load.stdout

  # Step 4.1 — Load the matrix (count + ids; verifies the embedded SHA256 exists).
  - id: load-matrix
    command: node atlas-requirement-walker.js --action=load --build-id="$LOBSTER_ARG_BUILD_ID"
    env:
      MATRIX: $matrix-of-build.stdout
    on_error: continue

  # Step 4.3 — Walker iteration: present the next un-vetted row (or signal done),
  #            derived from matrix + the decisions already recorded.
  - id: walker-iteration
    command: node atlas-requirement-walker.js --action=present-next --build-id="$LOBSTER_ARG_BUILD_ID"
    env:
      MATRIX: $matrix-of-build.stdout
      DECISIONS: $decisions-of-build.stdout
    on_error: continue

  # Step 4.7 — Loop bookkeeping: completed vs total (drives the progress record +
  #            the finalize branch).
  - id: check-loop
    command: node atlas-requirement-walker.js --action=check-loop --build-id="$LOBSTER_ARG_BUILD_ID"
    env:
      MATRIX: $matrix-of-build.stdout
      DECISIONS: $decisions-of-build.stdout
    on_error: continue

  # Step 4.2 — Vet progress record (replaces the taskr create/update). Load-merge-
  #            save into the per-build map; preserves started_at across runs. The
  #            merge defaults its inputs (NO on_error: continue — feeds state.set).
  - id: vetprog-load
    pipeline: state.get atlas/vet-progress

  - id: vetprog-next
    command: >
      node -e 'const map=JSON.parse(process.env.MAP||"null")||{};
        const id=process.env.LOBSTER_ARG_BUILD_ID||"build_unknown";
        const meta=JSON.parse(process.env.META||"null")||{};
        const chk=JSON.parse(process.env.CHECK||"null")||{};
        const now=new Date().toISOString();
        const prev=map[id]||{};
        map[id]={total_requirements:(chk.total_count!=null?chk.total_count:(meta.requirement_count||0)),
          decided_count:chk.completed_count||0,
          more_outstanding:chk.more_outstanding!==false,
          started_at:prev.started_at||now,
          updated_at:now};
        process.stdout.write(JSON.stringify(map));'
    env:
      MAP: $vetprog-load.stdout
      META: $load-matrix.stdout
      CHECK: $check-loop.stdout

  - id: vetprog-save
    pipeline: state.set atlas/vet-progress
    stdin: $vetprog-next.stdout

  # Step 4.4 — Surface the next row as a vet_row intent for the agent loop. The
  #            loop captures the user's approve/change/drop + trigger/classification
  #            edits, normalizes via apply-trigger-edits, writes the decision into
  #            atlas/decisions[build_id][req_id], then re-invokes this pipeline.
  #            NO on_error: continue on the -next (feeds the notifications state.set).
  - id: vetrow-notify-load
    condition: $walker-iteration.json.more_outstanding == true
    pipeline: state.get atlas/notifications

  - id: vetrow-notify-next
    condition: $walker-iteration.json.more_outstanding == true
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const w=JSON.parse(process.env.WALK||"null")||{};
        n.push({kind:"vet_row", channel:"atlas-vet",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          payload:{req_id:w.req_id, classification:w.classification,
            requirement_text:w.requirement_text, rationale:w.rationale,
            triggers:w.triggers||[],
            ask:"Vet this requirement one-by-one: approve / change (triggers and/or classification) / drop (with why). When classification is deterministic you MUST approve the triggers as drafted, edit them, or reclassify to llm/hybrid (S9.5 will refuse to advance otherwise).",
            persist_to:"atlas/decisions",
            instructions:"Normalize the decision via atlas-requirement-walker.js --action=apply-trigger-edits --build-id="+process.env.LOBSTER_ARG_BUILD_ID+" --req-id="+w.req_id+" (decision JSON on stdin), write it into atlas/decisions keyed by build_id then req_id, then re-invoke vet-walk for the next row."},
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $vetrow-notify-load.stdout
      WALK: $walker-iteration.stdout

  - id: vetrow-notify-save
    condition: $walker-iteration.json.more_outstanding == true
    pipeline: state.set atlas/notifications
    stdin: $vetrow-notify-next.stdout

  # Step 4.9 — Finalize: when no rows remain, queue the completion summary. (No
  #            n8n self-trigger — the agent loop drove each row to here.)
  - id: finalize-notify-load
    condition: $walker-iteration.json.more_outstanding == false
    pipeline: state.get atlas/notifications

  - id: finalize-notify-next
    condition: $walker-iteration.json.more_outstanding == false
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const chk=JSON.parse(process.env.CHECK||"null")||{};
        n.push({kind:"vet_complete", channel:"atlas-vet",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          summary:"Step 4 vet complete: "+(chk.completed_count||0)+" of "+(chk.total_count||0)+" requirements vetted. Step 5 recap incoming.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $finalize-notify-load.stdout
      CHECK: $check-loop.stdout

  - id: finalize-notify-save
    condition: $walker-iteration.json.more_outstanding == false
    pipeline: state.set atlas/notifications
    stdin: $finalize-notify-next.stdout

  # Audit log — always appended (the per-row decision write is the agent loop's
  # job). Records which row was presented this run. Defaults all inputs.
  - id: audit-load
    pipeline: state.get atlas/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const w=JSON.parse(process.env.WALK||"null")||{};
        const chk=JSON.parse(process.env.CHECK||"null")||{};
        log.push({action:"vet_iteration_presented",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          req_id:w.req_id||null,
          more_outstanding:w.more_outstanding!==false,
          decided_count:chk.completed_count||0,
          total_count:chk.total_count||0,
          timestamp:new Date().toISOString()});
        if (log.length>500) log.splice(0,log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      WALK: $walker-iteration.stdout
      CHECK: $check-loop.stdout

  - id: audit-save
    pipeline: state.set atlas/audit-log
    stdin: $audit-next.stdout
