name: build

# Step 6: File generation with pre-write scans + a hard S10 file-write approval gate +
# atomic write + register the built agent via the backend bridge (the create step Atlas
# cannot do gateway-free in-container). Triggered after S9 architecture sign-off clears.
#
# Gateway-free contract (see intake-and-research.lobster header):
#  - The scans (ClawHavoc, sensitive-data) run as bare `node` programs.
#  - The S10 hard confirm gate is a native `approval:` block; ALL file work gates on
#    `$file-write-gate.approved`. The old per-flag agentgates (A14 security exception,
#    sensitive dismissal) collapse into this one authoritative gate — the human sees the
#    scan flags + file list + bundle SHA256 in the prompt and approves the whole write.
#  - The agent's files are assembled from the per-build matrix the agent loop enriched
#    (atlas/requirement-matrix[build_id]) + the intake + brief, then materialized to
#    `workspace-atlas-staging-{build_id}/` under the EFS user root (ISOL8_AGENTS_DIR =
#    /home/node/.openclaw) where files land under `workspace/`.
#  - ATLAS NEVER PICKS AN AGENT ID. Every path here is keyed by the build's staging
#    token; the id is assigned by the bridge (Paperclip mint) and comes back in the
#    response. The staging root is deliberately the EFS user root and NOT
#    `~/.openclaw/workspaces`, which `openclaw doctor --fix` deletes on every boot.
#  - CREATE is a curl to the backend bridge (register-built-agent), which snapshots the
#    staging workspace, mints a Paperclip agent, and deploys it live. ${OPENCLAW_HOOK_TOKEN}
#    is the per-container webhook token (a real process env var). The bridge URL on the
#    register step is a literal double-brace placeholder substituted per-env/per-owner at
#    stage time by pre_stage_atlas (so this comment doesn't repeat the exact token).
#  - This pipeline does NOT publish to the marketplace — the OWNER publishes via the seller
#    wizard. It ends at register + a notification telling the owner which agent_id to publish.

args:
  build_id:
    default: ""
  agent_name:
    default: ""

steps:
  # Step 6.1 — ClawHavoc denylist scan (HARD; the security override is folded into the
  #            S10 gate below rather than a separate agentgate).
  - id: clawhavoc-scan
    command: node atlas-clawhavoc-enforcer.js --mode=build --build-id="$LOBSTER_ARG_BUILD_ID"
    env:
      ISOL8_AGENTS_DIR: /home/node/.openclaw
    on_error: continue

  # Step 6.2 — Sensitive-data sniff (WARN; surfaced in the gate prompt, audit-logged).
  - id: sensitive-sniff
    command: node atlas-sensitive-sniff.js --mode=build --build-id="$LOBSTER_ARG_BUILD_ID"
    env:
      ISOL8_AGENTS_DIR: /home/node/.openclaw
    on_error: continue

  # Step 6.3 — S10 file-write gate (sync hard block). Presents the scan flags, the file
  #            list, and the bundle SHA256. ALL downstream file work gates on its approval.
  - id: file-write-gate
    approval:
      required: true
      prompt: "Confirm agent file write for build $LOBSTER_ARG_BUILD_ID (agent: $LOBSTER_ARG_AGENT_NAME). ClawHavoc flagged: $clawhavoc-scan.json.violations. Sensitive-data warnings: $sensitive-sniff.json.warnings. Files: $sensitive-sniff.json.bundle_file_list. Bundle SHA256: $sensitive-sniff.json.bundle_sha256. Approve to write and register, or reject to revise."

  # Step 6.4 — Load the per-build inputs the agent loop enriched: the requirement matrix
  #            (skeleton + classifications + synthesis output), the brief, and the intake.
  - id: load-matrix
    condition: $file-write-gate.approved
    pipeline: state.get atlas/requirement-matrix

  - id: load-briefs
    condition: $file-write-gate.approved
    pipeline: state.get atlas/research-briefs

  - id: load-intakes
    condition: $file-write-gate.approved
    pipeline: state.get atlas/intakes

  # Step 6.5 — Assemble the full agent spec for the generator (intake + brief + the
  #            agent-loop synthesis output: user_stories, voice_tone, triggers, etc.).
  - id: assemble-spec
    condition: $file-write-gate.approved
    command: >
      node -e 'const id=process.env.LOBSTER_ARG_BUILD_ID;
        const m=(JSON.parse(process.env.MAP||"null")||{})[id]||{};
        const intake=(JSON.parse(process.env.INTAKES||"null")||{})[id]||{};
        const brief=(JSON.parse(process.env.BRIEFS||"null")||{})[id]||{};
        const sk=m.skeleton||{};
        process.stdout.write(JSON.stringify({build_id:id, intake, brief,
          user_stories:m.user_stories||[], voice_tone:m.voice_tone||{},
          requirements_with_triggers:m.requirements_with_triggers||[],
          pipelines:m.pipelines||[], scripts:m.scripts||[],
          skill_recommendations:m.skill_recommendations||[],
          what_you_are:sk.what_you_are}));'
    env:
      MAP: $load-matrix.stdout
      BRIEFS: $load-briefs.stdout
      INTAKES: $load-intakes.stdout

  # Step 6.6 — Draft the artifacts (returns artifacts_to_stage the write-disk step
  #            materializes). Separate from write-disk because write-disk reads the
  #            artifacts on stdin.
  - id: assemble-draft
    condition: $file-write-gate.approved
    command: node atlas-skeleton-generator.js --action=draft
    stdin: $assemble-spec.stdout
    on_error: continue

  # Step 6.7 — Atomic file generation via staging-dir-then-mv. ISOL8_AGENTS_DIR points at
  #            the EFS user root, so the build lands at
  #            `workspace-atlas-staging-{build_id}/` with files under `workspace/` —
  #            exactly where the bridge's export_built_agent_from_staging reads and tars
  #            (it resolves `{user root}/workspace-{staging_agent_id}`). No --agent-name:
  #            the destination is keyed by the BUILD, never by the name Atlas chose.
  - id: file-generate
    condition: $file-write-gate.approved
    command: node atlas-skeleton-generator.js --action=write-disk --build-id="$LOBSTER_ARG_BUILD_ID"
    stdin: $assemble-draft.stdout
    env:
      ISOL8_AGENTS_DIR: /home/node/.openclaw
    on_error: continue

  # Step 6.8 — Re-validate the promoted openclaw.json against the installed schema
  #            and record the post-write file count + per-file SHA256. The digests
  #            are a record, NOT a comparison — nothing diffs them against the
  #            staged hashes. `success` here gates the bridge call below.
  - id: verify-write
    condition: $file-write-gate.approved
    command: node atlas-skeleton-generator.js --action=verify-write --build-id="$LOBSTER_ARG_BUILD_ID"
    env:
      ISOL8_AGENTS_DIR: /home/node/.openclaw
    on_error: continue

  # Step 6.9 — Register the built agent via the backend bridge (the gateway-free CREATE).
  #            The bridge snapshots workspace-{staging_agent_id}/, mints a Paperclip agent,
  #            and deploys it into the container live. Returns {agent_id, registered:true} —
  #            THIS is where the agent's id comes from. Nothing before this step has one.
  - id: register-built-agent
    condition: $verify-write.json.success == true
    # Build the JSON body with node (JSON.stringify) and pipe it to curl --data @-,
    # rather than shell-interpolating $name into a -d string: an agent name with a
    # quote/backslash/newline would otherwise produce malformed JSON or split the
    # curl args. The bridge URL below is substituted per-owner at stage time.
    command: >
      node -e 'process.stdout.write(JSON.stringify({staging_agent_id:process.env.STAGING_ID,name:process.env.LOBSTER_ARG_AGENT_NAME,reports_to:null}))'
      | curl -sS -X POST "{{ISOL8_BRIDGE_URL}}"
      -H "Authorization: Bearer $OPENCLAW_HOOK_TOKEN"
      -H "Content-Type: application/json"
      --data @-
    env:
      # CONTRACT: the bridge resolves `{EFS user root}/workspace-{staging_agent_id}`,
      # so STAGING_ID must be the staging token write-disk promoted to. Both sides
      # derive it from the build id alone (atlas-skeleton-generator.js stagingIdFor),
      # so there is no name in the loop to drift.
      STAGING_ID: $verify-write.json.staging_agent_id
    on_error: continue

  # Step 6.10 — Register triggers (cron rows + hook/agent-call asserts) once registered.
  #            --agent-id is the id the BRIDGE assigned; cron rows keyed to anything
  #            else (the display name, say) address an agent that does not exist and
  #            never fire.
  - id: register-triggers
    condition: $register-built-agent.json.registered == true
    command: node atlas-trigger-registrar.js --mode=register --build-id="$LOBSTER_ARG_BUILD_ID" --agent-id="$register-built-agent.json.agent_id"
    env:
      ISOL8_AGENTS_DIR: /home/node/.openclaw
    on_error: continue

  # Step 6.12 — Central build status (always runs; defaults all inputs so it never refs a
  #            skipped producer). file_write_confirmed is derived from verify success — the
  #            file steps only run when the gate is approved. needs_rollback covers a failed
  #            register (after a good verify) OR a registered-but-trigger-failed build.
  - id: build-status
    on_error: continue
    command: >
      node -e 'const v=JSON.parse(process.env.VERIFY||"null")||{};
        const r=JSON.parse(process.env.REG||"null")||{};
        const t=JSON.parse(process.env.TRIG||"null")||{};
        const truthy=(s)=>["true","1","yes"].includes((s||"").trim().toLowerCase());
        const gate_approved=truthy(process.env.GATE);
        const verified=v.success===true;
        const registered=r.registered===true;
        const verify_failed=gate_approved && !verified;
        const triggers_ok=t.success===true;
        // !==true (not ===false) so a trigger-registrar CRASH (success undefined
        // under on_error: continue) is also treated as a failure — otherwise a
        // crash would be invisible (no rollback, no success notification).
        const needs_rollback=(verified && !registered) || (registered && !triggers_ok);
        process.stdout.write(JSON.stringify({verified, registered,
          agent_id:r.agent_id||null, triggers_ok,
          warnings:r.warnings||[],
          file_write_confirmed:verified, verify_failed, needs_rollback}));'
    env:
      VERIFY: $verify-write.stdout
      REG: $register-built-agent.stdout
      TRIG: $register-triggers.stdout
      GATE: $file-write-gate.approved

  # Step 6.12b — Deregister triggers when the agent registered but trigger-registration
  #            did not cleanly succeed (cleans cron rows written this build). Gated on
  #            build-status.triggers_ok == false (which is false for BOTH a clean
  #            {success:false} AND a crash where success is undefined) — running after
  #            build-status lets us use that computed flag instead of the register-triggers
  #            success field, which a crash leaves null (so == false would miss it).
  - id: deregister-triggers
    condition: "$build-status.json.registered == true && $build-status.json.triggers_ok == false"
    command: node atlas-trigger-registrar.js --mode=deregister --build-id="$LOBSTER_ARG_BUILD_ID" --agent-id="$build-status.json.agent_id"
    env:
      ISOL8_AGENTS_DIR: /home/node/.openclaw
    on_error: continue

  # Step 6.13a — Verify-failure notification. When the gate WAS approved (write was
  #            attempted) but verify-write failed (atomic rename / hash mismatch /
  #            schema violation), no bridge call is made and needs_rollback is false —
  #            so without this the build would stall silently. verify_failed is gated
  #            on gate-approval so a deliberate gate REJECTION (write never attempted)
  #            does NOT notify here. Reads verify-write AND file-generate directly
  #            (not build-status, which reduces them to booleans) so the notification
  #            carries the error kind + the exact schema_issues[] — a generic "retry"
  #            with the issues dropped means a blind retry reproduces the same
  #            violation. write-disk schema-gates the PROMOTE (nothing invalid is
  #            renamed into place), so a schema refusal surfaces at verify as
  #            destination_dir_missing with the issue list in file-generate's
  #            stdout — pull issues from whichever step caught them.
  #            (No on_error: a throwing -next must halt before -save, not wipe the queue.)
  - id: verify-fail-load
    condition: $build-status.json.verify_failed
    pipeline: state.get atlas/notifications

  - id: verify-fail-next
    condition: $build-status.json.verify_failed
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const v=JSON.parse(process.env.VERIFY||"null")||{};
        const w=JSON.parse(process.env.WRITE||"null")||{};
        const src=(w.schema_issues&&w.schema_issues.length)?w:v;
        const iss=src.schema_issues||[];
        const shown=iss.slice(0,5).map(i=>(i.path||"(config)")+": "+i.message);
        const more=iss.length>5?(" (+"+(iss.length-5)+" more)"):"";
        const detail=iss.length
          ? "Schema validation failed: "+shown.join(" | ")+more+". Nothing was promoted or registered; fix the config and rebuild."
          : "No files on disk or hash mismatch. Retry the build.";
        n.push({kind:"build_verify_failed", channel:"atlas-approvals",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          agent_name:process.env.LOBSTER_ARG_AGENT_NAME,
          error:src.error||null,
          schema_issues:iss.slice(0,5),
          summary:"File-write verification failed ("+(src.error||"unknown")+") for agent "+process.env.LOBSTER_ARG_AGENT_NAME+". "+detail+" No bridge call was made and no agent was created.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $verify-fail-load.stdout
      VERIFY: $verify-write.stdout
      WRITE: $file-generate.stdout

  - id: verify-fail-save
    condition: $build-status.json.verify_failed
    pipeline: state.set atlas/notifications
    stdin: $verify-fail-next.stdout

  # Step 6.13 — Rollback notification when the build needs it. The bridge already rolled
  #            back its own Paperclip mint + deploy on failure; the only local artifact is
  #            the inert staging workspace dir (not registered in openclaw.json, so it does
  #            nothing). So we surface a notice rather than rm -rf the dir — the simpler
  #            correct option (no destructive path handling in the pipeline).
  - id: rollback-load
    condition: $build-status.json.needs_rollback
    pipeline: state.get atlas/notifications

  # NOTE: the notification-append (-next) steps below have NO `on_error: continue`
  # on purpose — a throwing append must halt the pipeline so its -save never
  # overwrites atlas/notifications with empty stdin. build-status (which feeds
  # conditions, not a state.set) keeps on_error: continue. See
  # intake-and-research.lobster for the full rationale.
  - id: rollback-next
    condition: $build-status.json.needs_rollback
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const s=JSON.parse(process.env.STATUS||"null")||{};
        const name=process.env.LOBSTER_ARG_AGENT_NAME;
        const summary=s.registered
          ? "Triggers failed to register for agent "+name+" (agent_id: "+(s.agent_id||"unknown")+"). The agent IS registered in Paperclip but its cron/hooks are not active. Deregistration was attempted — retry trigger registration, or delete the agent and rebuild."
          : "Build failed to register with the bridge. The bridge rolled back its own Paperclip mint/deploy; the local staging workspace is inert. Retry the build.";
        n.push({kind:"build_rollback", channel:"atlas-approvals",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          agent_name:name, agent_id:s.agent_id||null,
          summary, created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $rollback-load.stdout
      STATUS: $build-status.stdout

  - id: rollback-save
    condition: $build-status.json.needs_rollback
    pipeline: state.set atlas/notifications
    stdin: $rollback-next.stdout

  # Step 6.14 — Success notification. The agent is registered but NOT listed — the OWNER
  #            publishes it via the seller wizard. Surface the agent_id so Atlas can tell
  #            the operator exactly which agent to publish.
  - id: success-load
    condition: "$build-status.json.registered == true && $build-status.json.triggers_ok == true"
    pipeline: state.get atlas/notifications

  - id: success-next
    condition: "$build-status.json.registered == true && $build-status.json.triggers_ok == true"
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const s=JSON.parse(process.env.STATUS||"null")||{};
        const warn=(s.warnings&&s.warnings.length)?(" Warnings: "+s.warnings.join(" | ")):"";
        n.push({kind:"build_registered", channel:"atlas-approvals",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          agent_name:process.env.LOBSTER_ARG_AGENT_NAME,
          agent_id:s.agent_id,
          summary:"Agent "+process.env.LOBSTER_ARG_AGENT_NAME+" registered (agent_id: "+s.agent_id+"). Open the marketplace publish wizard and pick this agent to list it."+warn,
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $success-load.stdout
      STATUS: $build-status.stdout

  - id: success-save
    condition: "$build-status.json.registered == true && $build-status.json.triggers_ok == true"
    pipeline: state.set atlas/notifications
    stdin: $success-next.stdout

  # Audit log — always runs; defaults all inputs. Records the gate, scan, write, and
  #            register outcomes (timestamp computed in-node, no template placeholder).
  - id: audit-load
    pipeline: state.get atlas/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const cs=JSON.parse(process.env.CLAW||"null")||{};
        const ss=JSON.parse(process.env.SNIFF||"null")||{};
        const s=JSON.parse(process.env.STATUS||"null")||{};
        log.push({action:"build_complete",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          agent_name:process.env.LOBSTER_ARG_AGENT_NAME,
          clawhavoc_pass: cs.pass===true,
          sensitive_warnings: ss.warnings||[],
          bundle_sha256: ss.bundle_sha256||null,
          file_write_confirmed: s.file_write_confirmed===true,
          atomic_mv_succeeded: s.verified===true,
          registered: s.registered===true,
          agent_id: s.agent_id||null,
          register_triggers_success: s.triggers_ok===true,
          needs_rollback: s.needs_rollback===true,
          timestamp:new Date().toISOString()});
        if (log.length>500) log.splice(0,log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      CLAW: $clawhavoc-scan.stdout
      SNIFF: $sensitive-sniff.stdout
      STATUS: $build-status.stdout

  - id: audit-save
    pipeline: state.set atlas/audit-log
    stdin: $audit-next.stdout
