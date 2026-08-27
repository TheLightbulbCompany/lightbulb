name: recap-and-cost

# Step 5: Recap (W5 SHA256 hash verification) + cost-impact estimate + S9
# architecture sign-off + S9.5 determinism audit.
#
# Gateway-free contract (see intake-and-research.lobster header): the recap/cost
# tools run as bare `node` programs reading the requirement matrix + decisions
# via env (extracted from the per-build maps); the hard confirm gate is a native
# `approval:` block; recap/cost/notice persistence is native state.set; Slack
# surfaces are queued onto atlas/notifications for the agent loop. state.* keys
# here: atlas/requirement-matrix, atlas/decisions, atlas/recaps,
# atlas/cost-estimates, atlas/notifications, atlas/audit-log.
#
# Determinism-audit restructure: the audit script no longer process.exit(1)s on
# failure (which used to halt the pipeline BEFORE the failure-notice — conditioned
# on the same failure — could run). It now reports pass/fail on stdout, and a
# following step branches on `$architecture-signoff.approved == true &&
# !$determinism-audit.json.pass` — the audit only runs after sign-off, so we gate
# on it having run (signoff approved) AND a falsy pass (explicit false OR a crash
# leaving pass null). Without the signoff guard, a skipped audit (hash mismatch /
# sign-off rejected → pass null → !null true) would spuriously report a failure.

args:
  build_id:
    default: ""

steps:
  # Load the per-build requirement matrix (architect-proposal + agent-loop
  # enriched) and the per-build decisions (vet-walk), then extract THIS build's
  # objects so each tool gets a single JSON blob on env (never a .json.field).
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

  # Step 5.1 — Verify SHA256 of the requirement matrix (W5). Reads the matrix on
  #            env; the stamped matrix_sha256 is verified against the recomputed
  #            deterministic core.
  - id: verify-matrix-hash
    command: node atlas-recap-diff.js --action=verify-hash --build-id="$LOBSTER_ARG_BUILD_ID"
    env:
      MATRIX: $matrix-of-build.stdout
    on_error: continue

  # Step 5.1b — Surface a hash-mismatch abort (W5) so the failure is observable
  #             rather than a silent skip of the recap.
  - id: hash-fail-load
    condition: $verify-matrix-hash.json.hash_matches == false
    pipeline: state.get atlas/notifications

  - id: hash-fail-next
    condition: $verify-matrix-hash.json.hash_matches == false
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const v=JSON.parse(process.env.VERIFY||"null")||{};
        n.push({kind:"recap_hash_mismatch", channel:"atlas-vet",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          summary:"Requirement-matrix SHA256 mismatch — the matrix was modified since snapshot. Recap aborted per W5. Re-run architect-proposal to re-snapshot before recap.",
          detail:v.error||null, created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $hash-fail-load.stdout
      VERIFY: $verify-matrix-hash.stdout

  - id: hash-fail-save
    condition: $verify-matrix-hash.json.hash_matches == false
    pipeline: state.set atlas/notifications
    stdin: $hash-fail-next.stdout

  # Step 5.2 — Produce the recap diff (original snapshot vs final decisions).
  - id: recap-diff
    command: node atlas-recap-diff.js --action=diff --build-id="$LOBSTER_ARG_BUILD_ID"
    env:
      MATRIX: $matrix-of-build.stdout
      DECISIONS: $decisions-of-build.stdout
    condition: $verify-matrix-hash.json.hash_matches == true
    on_error: continue

  # Step 5.3 — Persist the recap into the per-build map. Load-merge-save; the
  #            merge defaults its inputs so a crashed recap never wipes the map.
  - id: recaps-load
    condition: $verify-matrix-hash.json.hash_matches == true
    pipeline: state.get atlas/recaps

  - id: recaps-next
    condition: $verify-matrix-hash.json.hash_matches == true
    command: >
      node -e 'const map=JSON.parse(process.env.MAP||"null")||{};
        const id=process.env.LOBSTER_ARG_BUILD_ID||"build_unknown";
        const r=JSON.parse(process.env.RECAP||"null");
        if (r) map[id]=r;
        process.stdout.write(JSON.stringify(map));'
    env:
      MAP: $recaps-load.stdout
      RECAP: $recap-diff.stdout

  - id: recaps-save
    condition: $verify-matrix-hash.json.hash_matches == true
    pipeline: state.set atlas/recaps
    stdin: $recaps-next.stdout

  # Step 5.4 — Count deterministic vs LLM nodes (architectural ratio per L7).
  #            Gated on the hash match like the recap steps — never count cost on
  #            a matrix that failed W5 verification (modified since snapshot).
  - id: cost-count
    condition: $verify-matrix-hash.json.hash_matches == true
    command: node atlas-cost-counter.js --build-id="$LOBSTER_ARG_BUILD_ID"
    env:
      MATRIX: $matrix-of-build.stdout
    on_error: continue

  # Step 5.5 — Persist the cost estimate into the per-build map. Load-merge-save.
  - id: costs-load
    condition: $verify-matrix-hash.json.hash_matches == true
    pipeline: state.get atlas/cost-estimates

  - id: costs-next
    condition: $verify-matrix-hash.json.hash_matches == true
    command: >
      node -e 'const map=JSON.parse(process.env.MAP||"null")||{};
        const id=process.env.LOBSTER_ARG_BUILD_ID||"build_unknown";
        const c=JSON.parse(process.env.COST||"null");
        if (c) map[id]=c;
        process.stdout.write(JSON.stringify(map));'
    env:
      MAP: $costs-load.stdout
      COST: $cost-count.stdout

  - id: costs-save
    condition: $verify-matrix-hash.json.hash_matches == true
    pipeline: state.set atlas/cost-estimates
    stdin: $costs-next.stdout

  # Step 5.6 — Surface the recap + cost-impact card by queuing a notification for
  #            the agent loop (replaces the Slack post). Gated on a valid recap.
  - id: summary-notify-load
    condition: $verify-matrix-hash.json.hash_matches == true
    pipeline: state.get atlas/notifications

  - id: summary-notify-next
    condition: $verify-matrix-hash.json.hash_matches == true
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const r=JSON.parse(process.env.RECAP||"null")||{};
        const c=JSON.parse(process.env.COST||"null")||{};
        n.push({kind:"recap_cost_summary", channel:"atlas-vet",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          payload:{recap_summary:r.summary||null,
            replacement_count:c.replacement_count!=null?c.replacement_count:null,
            replacement_ratio:c.replacement_ratio!=null?c.replacement_ratio:null,
            cost_label:c.label||"projected",
            note:"Cost-impact is PROJECTED, NOT TESTED per H5; archetype-specific, not cross-archetype-comparable. Proceed to the S9 architecture sign-off?"},
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $summary-notify-load.stdout
      RECAP: $recap-diff.stdout
      COST: $cost-count.stdout

  - id: summary-notify-save
    condition: $verify-matrix-hash.json.hash_matches == true
    pipeline: state.set atlas/notifications
    stdin: $summary-notify-next.stdout

  # Step 5.7 — S9 architecture sign-off (native hard confirm). Only asked when a
  #            valid recap was produced.
  - id: architecture-signoff
    condition: $verify-matrix-hash.json.hash_matches == true
    approval:
      required: true
      prompt: "S9 architecture sign-off for build $LOBSTER_ARG_BUILD_ID. Recap: $recap-diff.json.summary. Cost-impact (PROJECTED, NOT TESTED per H5): replacement count $cost-count.json.replacement_count (~$cost-count.json.replacement_ratio%, archetype-specific). Approve to proceed to S9.5 determinism audit + Step 6 file generation, or reject to revise."

  # Step 5.8 — S9.5 determinism audit (HARD; no override per spec §5). Asserts
  #            every requirement classified `deterministic` has triggers AND each
  #            trigger's pipeline exists in the staging workspace. It reports
  #            pass/fail on stdout and NEVER halts (see header) so the failure
  #            notice below can run. LIGHTBULB_AGENTS_DIR resolves the staging dir the
  #            same way build.lobster does.
  - id: determinism-audit
    condition: $architecture-signoff.approved
    command: node atlas-determinism-audit.js --build-id="$LOBSTER_ARG_BUILD_ID"
    env:
      MATRIX: $matrix-of-build.stdout
      DECISIONS: $decisions-of-build.stdout
      LIGHTBULB_AGENTS_DIR: /home/node/.openclaw
    on_error: continue

  # Step 5.9 — Surface determinism-audit failures so the user knows what to fix.
  #            Now reachable because the audit no longer halts the pipeline.
  - id: det-fail-load
    condition: "$architecture-signoff.approved == true && !$determinism-audit.json.pass"
    pipeline: state.get atlas/notifications

  - id: det-fail-next
    condition: "$architecture-signoff.approved == true && !$determinism-audit.json.pass"
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const a=JSON.parse(process.env.AUDIT||"null");
        // Empty/unparseable AUDIT means the audit PROCESS died (OOM/SIGKILL)
        // before emitting — distinct from a clean zero-failure run. The script
        // itself try/catches thrown exceptions into {pass:false,error}, so a
        // null here is a process-level crash, not a 0-failure pass.
        const isCrash = !a || typeof a !== "object";
        const fails = isCrash ? [] : (a.failures||[]);
        const errMsg = isCrash ? "audit process died before emitting output" : (a.error||null);
        n.push({kind:"determinism_audit_failed", channel:"atlas-vet",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          payload:{failure_count:fails.length, failures:fails,
            error:errMsg,
            ask:errMsg
              ? "S9.5 determinism audit CRASHED ("+errMsg+") — no requirements were checked. Fix the audit tool / staging path and re-run recap before Step 6 build."
              : "S9.5 determinism audit failed: each listed requirement needs either a trigger binding or a reclassification to llm/hybrid. Re-vet, then re-run recap before Step 6 build."},
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $det-fail-load.stdout
      AUDIT: $determinism-audit.stdout

  - id: det-fail-save
    condition: "$architecture-signoff.approved == true && !$determinism-audit.json.pass"
    pipeline: state.set atlas/notifications
    stdin: $det-fail-next.stdout

  # Audit log — always appended. Defaults all inputs so it never references a
  # skipped/crashed producer. Timestamp computed in-node.
  - id: audit-load
    pipeline: state.get atlas/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const v=JSON.parse(process.env.VERIFY||"null")||{};
        const c=JSON.parse(process.env.COST||"null")||{};
        const a=JSON.parse(process.env.DET||"null")||{};
        const truthy=(s)=>["true","1","yes"].includes((s||"").trim().toLowerCase());
        log.push({action:"recap_and_cost_complete",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          matrix_hash_verified:v.hash_matches===true,
          replacement_ratio:c.replacement_ratio!=null?c.replacement_ratio:null,
          architecture_signoff:truthy(process.env.SIGNOFF),
          determinism_audit_pass:a.pass===true,
          determinism_audit_failures:a.failures||[],
          timestamp:new Date().toISOString()});
        if (log.length>500) log.splice(0,log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      VERIFY: $verify-matrix-hash.stdout
      COST: $cost-count.stdout
      DET: $determinism-audit.stdout
      SIGNOFF: $architecture-signoff.approved

  - id: audit-save
    pipeline: state.set atlas/audit-log
    stdin: $audit-next.stdout
