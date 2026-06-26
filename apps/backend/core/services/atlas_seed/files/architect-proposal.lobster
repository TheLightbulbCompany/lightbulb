name: architect-proposal

# Step 3: Architecture proposal — skeleton, classifier, skill recommend, archetype synthesis.
#
# Gateway-free contract (see intake-and-research.lobster header). The deterministic
# work (skeleton draft, det/LLM classification, skill recommend) runs as bare
# `node` programs; the archetype synthesis (1 LLM call) is queued as an intent on
# `atlas/notifications` for the agent loop, which writes user_stories / voice_tone /
# requirements_with_triggers back into `atlas/requirement-matrix[build_id]`.
# build.lobster reads that enriched matrix to materialize the agent's files.
# state.* keys here: `atlas/research-briefs`, `atlas/intakes`,
# `atlas/requirement-matrix`, `atlas/notifications`, `atlas/audit-log`.

args:
  build_id:
    default: ""

steps:
  # Step 3.1 + 3.2 — Load the research brief (from Step 2's agent-loop write) and the
  #            locked intake (from Step 1), both keyed by build_id inside their maps.
  - id: load-briefs
    pipeline: state.get atlas/research-briefs

  - id: load-intakes
    pipeline: state.get atlas/intakes

  - id: select-inputs
    command: >
      node -e 'const briefs=JSON.parse(process.env.BRIEFS||"null")||{};
        const intakes=JSON.parse(process.env.INTAKES||"null")||{};
        const id=process.env.LOBSTER_ARG_BUILD_ID||"";
        const brief=briefs[id]||null; const intake=intakes[id]||null;
        process.stdout.write(JSON.stringify({brief, intake,
          has_brief: brief?1:0, has_intake: intake?1:0}));'
    env:
      BRIEFS: $load-briefs.stdout
      INTAKES: $load-intakes.stdout

  # Step 3.2b — Missing-brief path: the agent loop has not produced a research brief
  #            for this build yet (H1). Queue a notice and skip the proposal; the
  #            always-run audit records the deferral.
  - id: nobrief-load
    condition: $select-inputs.json.has_brief == 0
    pipeline: state.get atlas/notifications

  - id: nobrief-next
    on_error: continue
    condition: $select-inputs.json.has_brief == 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        n.push({kind:"brief_not_ready", channel:"atlas-vet",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          summary:"No research brief found for this build yet. Run intake-and-research first, then re-run architect-proposal.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $nobrief-load.stdout

  - id: nobrief-save
    condition: $select-inputs.json.has_brief == 0
    pipeline: state.set atlas/notifications
    stdin: $nobrief-next.stdout

  # Step 3.3 — Generate the deterministic skeleton from {intake, brief}.
  - id: skeleton-input
    condition: $select-inputs.json.has_brief > 0
    command: >
      node -e 'const s=JSON.parse(process.env.SEL||"null")||{};
        process.stdout.write(JSON.stringify({intake:s.intake, brief:s.brief,
          build_id:process.env.LOBSTER_ARG_BUILD_ID}));'
    env:
      SEL: $select-inputs.stdout

  - id: skeleton-generate
    condition: $select-inputs.json.has_brief > 0
    command: node atlas-skeleton-generator.js --action=draft
    stdin: $skeleton-input.stdout
    on_error: continue

  # Step 3.4 — Classify each requirement deterministic vs LLM.
  - id: det-llm-classify
    condition: $select-inputs.json.has_brief > 0
    command: node atlas-det-llm-classifier.js
    stdin: $skeleton-generate.stdout
    on_error: continue

  # Step 3.5 — Recommend skill stack (refuses ClawHavoc-listed skills universally).
  - id: skill-input
    condition: $select-inputs.json.has_brief > 0
    command: >
      node -e 'const s=JSON.parse(process.env.SEL||"null")||{};
        process.stdout.write(JSON.stringify({skeleton:JSON.parse(process.env.SKEL||"null"),
          brief:s.brief}));'
    env:
      SEL: $select-inputs.stdout
      SKEL: $skeleton-generate.stdout

  - id: skill-recommend
    condition: $select-inputs.json.has_brief > 0
    command: node atlas-skill-recommender.js
    stdin: $skill-input.stdout
    on_error: continue

  # Step 3.6 — Archetype synthesis (1 LLM call) — queue an intent for the agent loop.
  #            The loop produces user_stories, voice_tone, AND per-requirement
  #            triggers[] binding, then merges them into atlas/requirement-matrix
  #            [build_id]. ALSO queue the user-facing audit surface in the same append.
  - id: notify-load
    condition: $select-inputs.json.has_brief > 0
    pipeline: state.get atlas/notifications

  - id: notify-next
    on_error: continue
    condition: $select-inputs.json.has_brief > 0
    command: >
      node -e 'const n=JSON.parse(process.env.NOTIFS||"null")||[];
        const id=process.env.LOBSTER_ARG_BUILD_ID;
        const sk=JSON.parse(process.env.SKEL||"null")||{};
        const sel=JSON.parse(process.env.SEL||"null")||{};
        n.push({kind:"archetype_synthesis", channel:"atlas-vet", build_id:id,
          payload:{skeleton:sk, classifications:JSON.parse(process.env.CLASS||"null"),
            skill_recommendations:JSON.parse(process.env.SKILLS||"null"),
            intake:sel.intake||null,
            persist_to:"atlas/requirement-matrix",
            instructions:"For each requirement: phrase as a user-story and propose triggers[] (cron | hook | agent-call). triggers MUST be non-empty when classification is deterministic. Also produce SOUL.md voice/tone. Merge user_stories, voice_tone, and requirements_with_triggers into atlas/requirement-matrix keyed by build_id."},
          created_at:new Date().toISOString()});
        n.push({kind:"architect_audit", channel:"atlas-vet", build_id:id,
          summary:"Step 3 deterministic skeleton complete ("+(sk.requirement_count||0)+" requirements). Archetype synthesis queued; Step 4 vet starts on confirmation.",
          created_at:new Date().toISOString()});
        process.stdout.write(JSON.stringify(n));'
    env:
      NOTIFS: $notify-load.stdout
      SKEL: $skeleton-generate.stdout
      CLASS: $det-llm-classify.stdout
      SKILLS: $skill-recommend.stdout
      # Pass the raw stdout blob (a JSON string), not .json.intake (a nested
      # object) — env vars are strings; a non-scalar .json path stringifies to
      # "[object Object]" and JSON.parse throws, silently dropping the intent.
      SEL: $select-inputs.stdout

  - id: notify-save
    condition: $select-inputs.json.has_brief > 0
    pipeline: state.set atlas/notifications
    stdin: $notify-next.stdout

  # Step 3.7 + 3.8 — Snapshot the deterministic requirement matrix into the per-build
  #            map and write its SHA256 (computed in-node via crypto — no $step.sha256
  #            pseudo-accessor). The agent loop later merges synthesis output into the
  #            same map[build_id]. Load-before-use, set [build_id], write.
  - id: compute-matrix
    condition: $select-inputs.json.has_brief > 0
    command: >
      node -e 'const crypto=require("crypto");
        const matrix={skeleton:JSON.parse(process.env.SKEL||"null"),
          classifications:JSON.parse(process.env.CLASS||"null"),
          skill_recommendations:JSON.parse(process.env.SKILLS||"null"),
          snapshot_at:new Date().toISOString()};
        const sha=crypto.createHash("sha256").update(JSON.stringify(matrix)).digest("hex");
        const sk=JSON.parse(process.env.SKEL||"null")||{};
        process.stdout.write(JSON.stringify({matrix:{...matrix, matrix_sha256:sha},
          matrix_sha256:sha, requirement_count:sk.requirement_count||0}));'
    env:
      SKEL: $skeleton-generate.stdout
      CLASS: $det-llm-classify.stdout
      SKILLS: $skill-recommend.stdout

  - id: matrix-load
    condition: $select-inputs.json.has_brief > 0
    pipeline: state.get atlas/requirement-matrix

  - id: matrix-next
    on_error: continue
    condition: $select-inputs.json.has_brief > 0
    command: >
      node -e 'const map=JSON.parse(process.env.MAP||"null")||{};
        const id=process.env.LOBSTER_ARG_BUILD_ID||"build_unknown";
        const c=JSON.parse(process.env.COMPUTED||"null")||{};
        map[id]={...(map[id]||{}), ...(c.matrix||{})};
        process.stdout.write(JSON.stringify(map));'
    env:
      MAP: $matrix-load.stdout
      COMPUTED: $compute-matrix.stdout

  - id: matrix-save
    condition: $select-inputs.json.has_brief > 0
    pipeline: state.set atlas/requirement-matrix
    stdin: $matrix-next.stdout

  # Audit log — always runs; defaults its inputs so it never references a skipped
  #            producer. Records the matrix SHA256 + requirement count (or the deferral).
  - id: audit-load
    pipeline: state.get atlas/audit-log

  - id: audit-next
    on_error: continue
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const sel=JSON.parse(process.env.SEL||"null")||{};
        const c=JSON.parse(process.env.COMPUTED||"null")||{};
        log.push({action:"architect_proposal_complete",
          build_id:process.env.LOBSTER_ARG_BUILD_ID,
          has_brief: sel.has_brief===1,
          matrix_sha256: c.matrix_sha256||null,
          requirement_count: c.requirement_count!=null?c.requirement_count:null,
          timestamp:new Date().toISOString()});
        if (log.length>500) log.splice(0,log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      SEL: $select-inputs.stdout
      COMPUTED: $compute-matrix.stdout

  - id: audit-save
    pipeline: state.set atlas/audit-log
    stdin: $audit-next.stdout
