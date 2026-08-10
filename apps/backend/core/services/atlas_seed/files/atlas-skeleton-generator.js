#!/usr/bin/env node
// atlas-skeleton-generator.js
//
// Generates ALL artifact files for new agents Atlas builds: openclaw.json + 6 .md
// workspace files + .lobster pipelines + .js Node scripts.
//
// Three modes:
//   --action=draft (default) — returns all agent files as a JSON artifacts map
//   --action=write-disk — materializes the drafted artifacts into a staging dir
//                         then atomic-mv to the agent dir (staging-dir-then-mv per W7)
//   --action=verify-write — post-write verification
//
// PRD format enforcement (R1/R3/R4):
//   R1: user-story format with required {{outcome}} field
//   R3: contract-violation comment in template
//   R4: empty-boundary refusal at write time + exact heading match enforcement
//
// writeDisk + verifyWrite carry from v0 unchanged.

const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');
const { execFileSync } = require('child_process');

const REQUIRED_HEADING_NEVER_DOES = 'What [Agent] Never Does — Non-Negotiable';
const REQUIRED_PRD_TEMPLATE = 'As a {{agent_name}}, I should {{action}} so that {{outcome}}';

// Gateway-free skill core for agents Atlas builds. Excludes fast-io / taskr /
// summarize / n8n-workflow: those route through the gateway tool-call proxy,
// which 401s on the embedded runner the built agents deploy to. Durable state
// uses native lobster state.get/state.set instead. The genuine gateway-free
// integrations (gog, slack) plus the security/capability stack remain.
const UNIVERSAL_SKILL_CORE = Object.freeze([
  'gog', 'slack',
  'capability-evolver', 'skill-vetter', 'sona-security-audit', 'agentgate'
]);

function parseArgs(argv) {
  const args = { action: 'draft', buildId: null, agentName: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--action=')) args.action = a.split('=')[1];
    else if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a.startsWith('--agent-name=')) args.agentName = a.split('=')[1];
  }
  return args;
}

function readStdin() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8')); } catch (e) { return {}; }
}

function renderUserStory(reqId, agentName, action, outcome) {
  if (!outcome || outcome.trim() === '') {
    throw new Error(`R1 violation: requirement ${reqId} has empty outcome field. Refusing to render.`);
  }
  return REQUIRED_PRD_TEMPLATE
    .replace('{{agent_name}}', agentName)
    .replace('{{action}}', action)
    .replace('{{outcome}}', outcome);
}

// --- TEMPLATE GENERATORS ---

function generateOpenclawJson(spec) {
  const agentName = spec.intake.name;
  const userSkills = spec.skills_required || [];
  const allSkills = [...new Set([...UNIVERSAL_SKILL_CORE, ...userSkills])];

  const skillEntries = {};
  for (const s of allSkills) skillEntries[s] = { enabled: true };

  // Only `name`, `description`, and `identity` {name,theme,emoji,avatar} are
  // schema-legal identity carriers on an agent entry (AgentEntrySchema +
  // IdentitySchema are .strict()). `description` feeds the Paperclip mint's
  // capabilities via the export manifest — without it the org chart shows a
  // blank agent. The archetype belongs in IDENTITY.md only; on the entry it
  // is a non-schema key the deploy sanitizer has to strip.
  const description = (spec.intake.vibe || spec.what_you_are || '').trim();

  const agentEntry = {
    id: agentName,
    name: agentName,
    workspace: `~/.openclaw/agents/${agentName}/workspace`,
    model: 'claude-haiku-4-5',
    thinkingDefault: 'low',
    identity: { name: agentName, emoji: spec.intake.emoji || '🛠' },
    skills: allSkills,
    tools: { profile: 'coding', alsoAllow: ['lobster', 'llm-task', 'message'] }
  };
  if (description) agentEntry.description = description;

  return JSON.stringify({
    agents: {
      defaults: {
        workspace: `~/.openclaw/agents/${agentName}/workspace`,
        timeoutSeconds: 600,
        blockStreamingDefault: 'off'
      },
      list: [agentEntry]
    },
    plugins: {
      entries: {
        lobster: { enabled: true },
        'llm-task': {
          enabled: true,
          config: {
            defaultProvider: 'anthropic',
            defaultModel: 'claude-haiku-4-5',
            maxTokens: 2000,
            timeoutMs: 45000
          }
        }
      }
    },
    skills: { entries: skillEntries },
    tools: { deny: spec.tools_deny || [] },
    cron: {
      enabled: true,
      store: '~/.openclaw/cron/jobs.json',
      maxConcurrentRuns: 2,
      retry: {
        maxAttempts: 3,
        backoffMs: [60000, 120000, 300000],
        retryOn: ['rate_limit', 'overloaded', 'network', 'server_error']
      },
      sessionRetention: '24h'
    },
    hooks: { enabled: true, token: '${OPENCLAW_HOOK_TOKEN}', path: '/hooks' },
    channels: { slack: { enabled: true } }
  }, null, 2);
}

function generateAgentsMd(spec) {
  const agentName = spec.intake.name;
  const userStories = (spec.user_stories || []).map(us =>
    `- **${us.req_id}** — ${renderUserStory(us.req_id, us.agent_name || agentName, us.action, us.outcome)}`
  ).join('\n');

  const neverDoes = spec.never_does || [];
  if (neverDoes.length === 0) {
    throw new Error(`R4 violation: ${agentName}'s "What [Agent] Never Does — Non-Negotiable" section is empty. Every agent must have at least one hard boundary. Refusing to render.`);
  }
  const neverDoesList = neverDoes.map(b => `- ${b}`).join('\n');
  const neverDoesHeading = REQUIRED_HEADING_NEVER_DOES.replace('[Agent]', agentName);

  // BSL appendix: starter content. Atlas's emitted agents inherit Atlas's six canonical instances
  // as the documented baseline; new catches during the agent's own builds get appended here.
  const bslAppendix = `## Build Session Learnings — Methodology Working As Designed

This appendix tracks citation/source verification catches per the H4 hard rule. Inherited from Atlas's own build history; new catches during this agent's own work get appended here.

Six canonical instances pre-populated from Atlas's bootstrap (see agents/atlas/AGENTS.md for full text):
1. P1 Trend Micro Feb 2026 attribution — DROPPED (HTTP 403, unverifiable)
2. H3 Choi et al. arxiv 2412.00804 — VERIFIED, RETAINED
3. S5 taskr "UI surface" claim — REMOVED (not in source)
4. CA5 Aider 85%-vs-61% — CORRECTED (conflated two studies)
5. IR5 arxiv 2501.10868 — DROPPED (real source, wrong attribution)
6. P2.3 Spracklen et al. — SUBCLAIM DROPPED (partial verification — headline claims verify, specific 43%/58% subclaim doesn't)

The generalization from #6: confirming a citation exists is not sufficient — each specific subclaim cited from it needs independent verification against the source content. The H4 rule applies at subclaim granularity, not just at source granularity.`;

  return `# AGENTS.md

## What You Are

${spec.what_you_are || `You are ${agentName}.`}

${bslAppendix}

## Requirements (User-Story Format)

<!-- R3 contract: drift between vetted gate-classification and AGENTS.md autonomy section is a contract violation, not a stylistic choice. Update both together or neither. -->

${userStories}

## Calibrated Autonomy

### Autonomous (no human gate)
${(spec.autonomous || []).map(a => `- ${a}`).join('\n') || '- (none specified — fill in during refinement)'}

### Gated (sync hard block, requires explicit confirmation)
${(spec.gated || []).map(g => `- ${g}`).join('\n') || '- (none specified — fill in during refinement)'}

## ${neverDoesHeading}

${neverDoesList}

## Documented Failure Cases This Agent Prevents

${(spec.failure_cases || []).map(c => `- **${c.title}** — ${c.description} (source: ${c.source && c.source.url ? c.source.url : 'in-house'})`).join('\n') || '- (none cited — fill in during refinement)'}
`;
}

function generateSoulMd(spec) {
  const vt = spec.voice_tone || {};
  return `# SOUL.md

## Identity
${vt.identity || spec.what_you_are || ''}

## Voice
${vt.voice || ''}

## Tone by Context
${vt.tone_by_context || ''}

## Boundaries
${vt.boundaries || ''}

## What You Care About
${vt.what_you_care_about || ''}
`;
}

function generateIdentityMd(spec) {
  return `name: ${spec.intake.name}
emoji: ${spec.intake.emoji || '🛠'}
vibe: ${spec.intake.vibe || ''}
archetype: ${spec.intake.archetype || 'general'}
build_id: ${spec.intake.build_id || ''}
`;
}

function generateToolsMd(spec) {
  const allSkills = [...new Set([...UNIVERSAL_SKILL_CORE, ...(spec.skills_required || [])])];
  return `# TOOLS.md

## Lobster Pipelines

${(spec.pipelines || []).map(p => `- \`${p.name}.lobster\` — ${p.purpose}`).join('\n')}

## llm-task

Used for structured LLM subtasks within pipelines. Always specify thinking level (off/low/medium/high), schema for structured output, minimal input. Never use llm-task when a Node script can do the job.

## Persistent State (lobster state.get / state.set)

Durable key/value state lives in the lobster runtime's native store — read with a
\`state.get <key>\` pipeline step and written with \`state.set <key>\` (the embedded
runner has no fast-io gateway tool; gateway tool calls 401). Use plain string keys
(no interpolation); to namespace per-run, key a map by the run id INSIDE the value.
Initial keys (expand as needed during refinement):
${(spec.fast_io_keys || []).map(k => `- \`${k.key}\` — ${k.purpose}`).join('\n') || '- (define state keys during refinement)'}

## Skills Installed

${allSkills.map(s => `- ${s}`).join('\n')}

## Node Scripts

${(spec.scripts || []).map(s => `- \`${s.name}.js\` — ${s.purpose}`).join('\n')}

## Click-to-Connect Integrations

Pattern: integrations declared \`enabled: false\` in openclaw.json with explanatory note; only mentioned to user when needed.

## Security

- skill-vetter — run against every skill before production
- sona-security-audit — runtime monitoring
- Denied skills listed in openclaw.json \`tools.deny\` (canonical source)
`;
}

function generateBootstrapMd(spec) {
  const agentName = spec.intake.name;
  return `# BOOTSTRAP.md

One-time setup for ${agentName}. Runs on first activation, then deletes itself.

## Step 1: Validate Prerequisites

Required environment variables:
- \`OPENCLAW_HOOK_TOKEN\`
${(spec.required_env_vars || []).map(v => `- \`${v}\``).join('\n')}

## Step 2: Initialize Default Pipeline State

Seed each persistent-state key with its default by piping the value into a
\`state.set <key>\` pipeline step (or let the first pipeline run create it lazily):
${(spec.fast_io_keys || []).map(k => `- \`${k.key}\` — default \`${k.default_value || '{}'}\``).join('\n') || '(define initial state during refinement)'}

## Step 3: Register Cron Jobs

${(spec.cron_jobs || []).map(c => `\`\`\`\nopenclaw cron add ${c.name} "${c.schedule}" "${c.command}" --session isolated --light-context\n\`\`\``).join('\n') || '(no cron jobs declared)'}

## Step 4: Verify the Security Stack

The security skills (\`skill-vetter\`, \`sona-security-audit\`, \`agentgate\`) are
enabled in \`openclaw.json\` under \`skills\` and load with the agent at boot — there
is no gateway invoke step on the embedded runner. Confirm each is present in the
agent's skill list and that \`openclaw.json\` \`tools.deny\` carries the denied-tool
list before going live.

## Step 5: Run Activation Check

\`\`\`
node ${agentName}-activation-check.js
\`\`\`

## Step 6: Announce Readiness and Delete This File

Post to user's status channel:
> "${agentName} is ready. See AGENTS.md for the operating contract."

Then: \`rm BOOTSTRAP.md\`.
`;
}

function generateUserMd(spec) {
  return `# USER.md

Holds the operator's profile. Replace placeholders with actual values.

## Identity
- name: <your name>
- role: <your role>
- timezone: <IANA timezone>
- preferred_name: <how the agent should address you>

## Communication preferences
- approval_channel: <Slack channel for approval gates>
- status_channel: <Slack channel for status updates>

## Notes
Free-text. Use this for context the agent should know about you and your preferences.
`;
}

// Slugify a build-supplied name to a safe token before embedding it in a
// generated .lobster (which becomes a shell-executed `node -e '...'` + state
// keys + filename). Pipeline/script names come from user requirements at build
// time, so an unsanitized `'` or shell metachar would break or inject into the
// generated command. Keep only [a-zA-Z0-9_-].
function _safeName(name, fallback) {
  return String(name || '').replace(/[^a-zA-Z0-9_-]/g, '-').replace(/^-+|-+$/g, '') || fallback;
}

function generateLobsterPipeline(pipeline) {
  // Real working starter file. Refined through the agent loop.
  //
  // Gateway-free contract (the embedded runner has NO gateway — gateway tool
  // calls 401 and shelling to the exec proxy exits 127): real programs run under
  // a bare `command: node x.js` (input via `stdin:`), durable state uses native
  // `state.get`/`state.set` (NOT a fast-io tool call), and the audit trail is a
  // load-append-save on a single state key. Refine the steps to the real workflow.
  const name = _safeName(pipeline.name, 'pipeline');
  const procScript = _safeName(pipeline.processing_script, 'PROCESSING_SCRIPT');
  return `name: ${name}

# ${pipeline.purpose}
# Refine the steps below based on the actual workflow this pipeline implements.

args:
${(pipeline.args || []).map(a => `  ${_safeName(a.name, 'arg')}:\n    default: "${String(a.default || '').replace(/["\\$]/g, '')}"`).join('\n')}

steps:
  # Step 1: Load durable input from native pipeline state.
  - id: load-input
    pipeline: state.get ${name}/input

  # Step 2: Run deterministic processing (refine to your needs). Bare node
  #         program; input arrives on stdin, the result is written to stdout.
  - id: process
    command: node ${procScript}.js
    stdin: $load-input.stdout
    on_error: continue

  # Step 3: Persist the result back to durable state.
  - id: persist
    pipeline: state.set ${name}/output
    stdin: $process.stdout

  # Audit log — load-append-save on a single state key. NO on_error: continue on
  # the append so a throwing append HALTS before its -save rather than overwriting
  # the shared audit array with empty stdin. Defaults its input so it never
  # references a crashed producer; timestamp computed in-node.
  - id: audit-load
    pipeline: state.get ${name}/audit-log

  - id: audit-next
    command: >
      node -e 'const log=JSON.parse(process.env.AUDIT||"null")||[];
        const r=JSON.parse(process.env.RESULT||"null")||{};
        log.push({action:"${name}_complete",
          result_summary:r.summary||null,
          timestamp:new Date().toISOString()});
        if (log.length>500) log.splice(0,log.length-500);
        process.stdout.write(JSON.stringify(log));'
    env:
      AUDIT: $audit-load.stdout
      RESULT: $process.stdout

  - id: audit-save
    pipeline: state.set ${name}/audit-log
    stdin: $audit-next.stdout
`;
}

function generateScriptArchetype(script) {
  // Real working starter file using the standard stdin-JSON → stdout-JSON convention.
  return `#!/usr/bin/env node
// ${script.name}.js
//
// ${script.purpose}
// Archetype: ${script.archetype || 'general'}
// Refine the implementation logic below based on this script's actual responsibilities.

const fs = require('fs');

function parseArgs(argv) {
  const args = {};
  for (const a of argv.slice(2)) {
    if (a.startsWith('--')) {
      const [key, val] = a.slice(2).split('=');
      args[key] = val !== undefined ? val : true;
    }
  }
  return args;
}

function readStdin() {
  try { return JSON.parse(fs.readFileSync(0, 'utf8')); } catch (e) { return {}; }
}

// Named processInput (NOT process) so it doesn't shadow Node's global
// process object — main() needs process.argv + process.stdout.write.
function processInput(input) {
  // TODO: Replace this stub with the actual logic for ${script.purpose}.
  // Refine based on the user/agent loop interaction.
  return {
    pass: true,
    input_received: !!input,
    note: 'Starter implementation; refine for ${script.purpose}.'
  };
}

function main() {
  const args = parseArgs(process.argv);
  const input = readStdin();
  const result = processInput(input);
  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) main();

module.exports = { processInput };
`;
}

function _flattenTriggers(requirementsWithTriggers) {
  const out = [];
  for (const row of (requirementsWithTriggers || [])) {
    for (const t of (row.triggers || [])) {
      out.push({ req_id: row.req_id, ...t });
    }
  }
  return out;
}

function _sha256OfString(s) {
  return crypto.createHash('sha256').update(s).digest('hex');
}

function _buildManifest(buildId, agentName, artifactsUnderWorkspace, triggers) {
  const files = Object.keys(artifactsUnderWorkspace).sort().map(p => ({
    path: p,
    sha256: _sha256OfString(artifactsUnderWorkspace[p])
  }));
  return {
    build_id: buildId,
    agent_name: agentName,
    generated_at: new Date().toISOString(),
    files,
    triggers
  };
}

function _buildTriggersJson(buildId, agentName, triggers) {
  // registered_as is filled in by the registrar at register-time; emit blank
  // here so the file shape matches what the registrar later mutates.
  return {
    agent_name: agentName,
    build_id: buildId,
    triggers: triggers.map(t => ({ ...t, registered_as: null }))
  };
}

function draftToFastIo(spec) {
  const agentName = spec.intake && spec.intake.name;
  const buildId = spec.build_id || (spec.intake && spec.intake.build_id) || 'build_unknown';
  const errors = [];

  let agentsMd, soulMd, identityMd, toolsMd, bootstrapMd, userMd, openclawJson;
  try { openclawJson = generateOpenclawJson(spec); } catch (e) { errors.push({ file: 'openclaw.json', error: e.message }); }
  try { agentsMd = generateAgentsMd(spec); } catch (e) { errors.push({ file: 'AGENTS.md', error: e.message }); }
  try { soulMd = generateSoulMd(spec); } catch (e) { errors.push({ file: 'SOUL.md', error: e.message }); }
  try { identityMd = generateIdentityMd(spec); } catch (e) { errors.push({ file: 'IDENTITY.md', error: e.message }); }
  try { toolsMd = generateToolsMd(spec); } catch (e) { errors.push({ file: 'TOOLS.md', error: e.message }); }
  try { bootstrapMd = generateBootstrapMd(spec); } catch (e) { errors.push({ file: 'BOOTSTRAP.md', error: e.message }); }
  try { userMd = generateUserMd(spec); } catch (e) { errors.push({ file: 'USER.md', error: e.message }); }

  if (errors.length > 0) {
    return { pass: false, errors };
  }

  // All generated agent files land under workspace/ — matches
  // openclaw.json#agents.defaults.workspace = "~/.openclaw/agents/{name}/workspace".
  const underWorkspace = {
    'workspace/openclaw.json': openclawJson,
    'workspace/AGENTS.md': agentsMd,
    'workspace/SOUL.md': soulMd,
    'workspace/IDENTITY.md': identityMd,
    'workspace/TOOLS.md': toolsMd,
    'workspace/BOOTSTRAP.md': bootstrapMd,
    'workspace/USER.md': userMd
  };

  for (const pipeline of (spec.pipelines || [])) {
    // Sanitize the filename the same way generateLobsterPipeline sanitizes the
    // workflow name + state keys, so the file, its `name:`, and its state keys align.
    underWorkspace[`workspace/${_safeName(pipeline.name, 'pipeline')}.lobster`] = generateLobsterPipeline(pipeline);
  }
  for (const script of (spec.scripts || [])) {
    underWorkspace[`workspace/${_safeName(script.name, 'script')}.js`] = generateScriptArchetype(script);
  }

  const flatTriggers = _flattenTriggers(spec.requirements_with_triggers);
  underWorkspace['workspace/triggers.json'] = JSON.stringify(
    _buildTriggersJson(buildId, agentName, flatTriggers), null, 2
  );

  // build-manifest.json lives at the staging ROOT, not under workspace/.
  // It documents the build itself (files + triggers + sha256s); it's not
  // shipped to the agent's runtime workspace.
  const manifest = _buildManifest(buildId, agentName, underWorkspace, flatTriggers);

  const artifacts = {
    ...underWorkspace,
    'build-manifest.json': JSON.stringify(manifest, null, 2)
  };

  return {
    pass: true,
    requirement_count: (spec.user_stories || []).length,
    trigger_count: flatTriggers.length,
    artifact_count: Object.keys(artifacts).length,
    artifacts_to_stage: artifacts
  };
}

// --- WRITE DISK (staging-dir-then-mv via fs.renameSync, atomic on POSIX) ---

// Materialize the drafted artifacts onto disk under `stagingDir`. `artifacts`
// is the { "<relpath>": "<string content>" } map returned by draftToFastIo as
// `artifacts_to_stage`. Creates parent dirs; refuses path traversal outside the
// staging dir. THIS is the create step that was missing — draft only RETURNED
// the artifacts as JSON, so nothing ever populated the staging dir that the
// atomic mv moves into place (every build hit `staging_dir_missing`).
function _materializeStaging(stagingDir, artifacts) {
  fs.mkdirSync(stagingDir, { recursive: true });
  const root = path.resolve(stagingDir) + path.sep;
  for (const [rel, content] of Object.entries(artifacts)) {
    const full = path.join(stagingDir, rel);
    if (!path.resolve(full).startsWith(root)) {
      throw new Error(`refusing to write outside staging dir: ${rel}`);
    }
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, typeof content === 'string' ? content : JSON.stringify(content, null, 2), 'utf8');
  }
}

function writeDisk(buildId, agentName, artifacts) {
  const agentsRoot = process.env.ISOL8_AGENTS_DIR || path.join(os.homedir(), '.openclaw', 'agents');
  const stagingDir = path.join(agentsRoot, `.atlas-staging-${buildId}`);

  if (artifacts && typeof artifacts === 'object' && Object.keys(artifacts).length) {
    try {
      _materializeStaging(stagingDir, artifacts);
    } catch (e) {
      // Clean up the partially-written staging dir so a retry starts clean —
      // matches the mv_failed path. Best-effort; report if cleanup also fails.
      let cleanupError = null;
      try {
        fs.rmSync(stagingDir, { recursive: true, force: true });
      } catch (ce) { cleanupError = ce.message; }
      const err = {
        success: false,
        error: 'staging_write_failed',
        message: `Failed to materialize artifacts into staging dir: ${e.message}`,
        build_id: buildId,
        staging_dir: stagingDir,
        destination_dir: null,
        cleanup_succeeded: !cleanupError,
        cleanup_error: cleanupError
      };
      process.stderr.write(JSON.stringify(err) + '\n');
      return err;
    }
  }

  if (!agentName) {
    // openclaw.json is staged under workspace/ (matches the artifact layout);
    // fall back to the staging root for the legacy flat layout.
    for (const candidate of [
      path.join(stagingDir, 'workspace', 'openclaw.json'),
      path.join(stagingDir, 'openclaw.json')
    ]) {
      try {
        const cfg = JSON.parse(fs.readFileSync(candidate, 'utf8'));
        agentName = cfg && cfg.agents && cfg.agents.list && cfg.agents.list[0] && cfg.agents.list[0].id;
        if (agentName) break;
      } catch (e) { /* try next candidate */ }
    }
  }
  if (!agentName) {
    const err = {
      success: false,
      error: 'agent_name_unresolvable',
      message: 'Could not read agents.list[0].id from openclaw.json in staging dir.',
      build_id: buildId,
      staging_dir: stagingDir,
      destination_dir: null
    };
    process.stderr.write(JSON.stringify(err) + '\n');
    return err;
  }
  const destDir = path.join(agentsRoot, agentName);

  if (!fs.existsSync(stagingDir)) {
    const err = {
      success: false,
      error: 'staging_dir_missing',
      message: `Staging dir does not exist: ${stagingDir}`,
      build_id: buildId,
      staging_dir: stagingDir,
      destination_dir: destDir
    };
    process.stderr.write(JSON.stringify(err) + '\n');
    return err;
  }

  // Compute per-file SHA256s BEFORE mv (for rollback error report)
  // Recursive walk — preserves staging-dir subdirectories (workspace/, skills/, etc.).
  const stagedHashes = {};
  function _walk(dir, relBase) {
    for (const dirent of fs.readdirSync(dir, { withFileTypes: true })) {
      const rel = relBase ? path.join(relBase, dirent.name) : dirent.name;
      const full = path.join(dir, dirent.name);
      if (dirent.isDirectory()) {
        _walk(full, rel);
      } else if (dirent.isFile()) {
        const content = fs.readFileSync(full);
        // Use forward-slash separators in the hash key so the manifest is
        // portable across host OSes (POSIX-style is what the spec uses).
        stagedHashes[rel.split(path.sep).join('/')] =
          crypto.createHash('sha256').update(content).digest('hex');
      }
    }
  }
  _walk(stagingDir, '');
  const stagedFileCount = Object.keys(stagedHashes).length;

  if (fs.existsSync(destDir)) {
    const err = {
      success: false,
      error: 'destination_exists',
      message: `Destination already exists: ${destDir}. Refusing to overwrite.`,
      build_id: buildId,
      staging_dir: stagingDir,
      destination_dir: destDir,
      staged_file_hashes: stagedHashes,
      rollback_attempted: false,
      rollback_message: 'Staging dir preserved for inspection; not auto-cleaned because dest-collision is operator-resolvable.'
    };
    process.stderr.write(JSON.stringify(err) + '\n');
    return err;
  }

  // Schema-gate the PROMOTE: validate the staged openclaw.json BEFORE the
  // atomic mv, so a rejected config never occupies the destination — otherwise
  // a corrected retry hits destination_exists and the build wedges. Staging is
  // cleaned so the retry regenerates from scratch. Fail-open (no openclaw bin)
  // is inherited from validateStagedConfig.
  const stagedCfg = [
    path.join(stagingDir, 'workspace', 'openclaw.json'),
    path.join(stagingDir, 'openclaw.json')
  ].find(p => fs.existsSync(p));
  const schemaCheck = stagedCfg ? validateStagedConfig(stagedCfg) : { valid: true, issues: [] };
  if (!schemaCheck.valid) {
    let cleanupError = null;
    try {
      fs.rmSync(stagingDir, { recursive: true, force: true });
    } catch (ce) { cleanupError = ce.message; }
    const err = {
      success: false,
      error: 'schema_validation_failed',
      message: 'Staged openclaw.json violates the installed OpenClaw schema. Nothing was promoted; fix schema_issues and rebuild.',
      schema_issues: schemaCheck.issues,
      build_id: buildId,
      staging_dir: stagingDir,
      destination_dir: destDir,
      staged_file_hashes: stagedHashes,
      cleanup_succeeded: !cleanupError,
      cleanup_error: cleanupError
    };
    process.stderr.write(JSON.stringify(err) + '\n');
    return err;
  }

  // Atomic mv via rename(2)
  try {
    fs.renameSync(stagingDir, destDir);
    return {
      success: true,
      build_id: buildId,
      staging_dir: stagingDir,
      destination_dir: destDir,
      agent_name: agentName,
      file_count: stagedFileCount,
      file_hashes: stagedHashes,
      mv_method: 'fs.renameSync (POSIX rename(2), atomic on local filesystem)',
      written_at: new Date().toISOString()
    };
  } catch (e) {
    let rollbackError = null;
    try {
      fs.rmSync(stagingDir, { recursive: true, force: true });
    } catch (re) { rollbackError = re.message; }

    const err = {
      success: false,
      error: 'mv_failed',
      message: `Atomic mv failed: ${e.message}`,
      mv_errno: e.errno || null,
      mv_code: e.code || null,
      build_id: buildId,
      staging_dir: stagingDir,
      destination_dir: destDir,
      staged_file_hashes: stagedHashes,
      rollback_attempted: true,
      rollback_succeeded: !rollbackError,
      rollback_error: rollbackError,
      rollback_message: rollbackError
        ? `rm -rf staging dir FAILED: ${rollbackError}. MANUAL CLEANUP REQUIRED at ${stagingDir}.`
        : `Staging dir cleaned up via rm -rf. No partial agent dir created.`,
      failed_at: new Date().toISOString()
    };
    process.stderr.write(JSON.stringify(err) + '\n');
    return err;
  }
}

// Schema-validate a staged openclaw.json against the INSTALLED OpenClaw's own
// zod schema (`openclaw config validate --json` on OPENCLAW_CONFIG_PATH). The
// agent-entry schema is .strict(), so an invented key silently bricks the
// config reload at deploy time — catch it here, before register. Fail-OPEN
// when the openclaw bin is absent (local dev / tests): the server-side
// register sanitizer remains the backstop there.
function validateStagedConfig(cfgPath) {
  try {
    execFileSync('openclaw', ['config', 'validate', '--json'], {
      env: { ...process.env, OPENCLAW_CONFIG_PATH: cfgPath },
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 30000
    });
    return { valid: true, issues: [] };
  } catch (e) {
    if (e.code === 'ENOENT') {
      return { valid: true, issues: [], skipped: 'openclaw bin not found' };
    }
    try {
      const parsed = JSON.parse(e.stdout);
      return { valid: false, issues: parsed.issues || [] };
    } catch (pe) {
      return { valid: false, issues: [{ path: '', message: String(e.message || e) }] };
    }
  }
}

function verifyWrite(buildId, agentName) {
  const agentsRoot = process.env.ISOL8_AGENTS_DIR || path.join(os.homedir(), '.openclaw', 'agents');
  const destDir = path.join(agentsRoot, agentName);
  if (!fs.existsSync(destDir)) {
    return {
      success: false,
      error: 'destination_dir_missing',
      message: `Agent dir not found post-write: ${destDir}`,
      build_id: buildId,
      destination_dir: destDir
    };
  }
  const observedHashes = {};
  function _walk(dir, relBase) {
    for (const dirent of fs.readdirSync(dir, { withFileTypes: true })) {
      const rel = relBase ? path.join(relBase, dirent.name) : dirent.name;
      const full = path.join(dir, dirent.name);
      if (dirent.isDirectory()) {
        _walk(full, rel);
      } else if (dirent.isFile()) {
        const content = fs.readFileSync(full);
        observedHashes[rel.split(path.sep).join('/')] =
          crypto.createHash('sha256').update(content).digest('hex');
      }
    }
  }
  _walk(destDir, '');
  const observedFileCount = Object.keys(observedHashes).length;

  // Schema check on the staged openclaw.json. workspace/ is the canonical
  // location; the staging root covers the legacy flat layout (same candidates
  // writeDisk resolves the agent name from). A failing check flips success to
  // false so the register gate in build.lobster blocks on the exact issue list.
  let schemaCheck = { valid: true, issues: [] };
  for (const candidate of [
    path.join(destDir, 'workspace', 'openclaw.json'),
    path.join(destDir, 'openclaw.json')
  ]) {
    if (fs.existsSync(candidate)) {
      schemaCheck = validateStagedConfig(candidate);
      break;
    }
  }

  const result = {
    success: schemaCheck.valid,
    build_id: buildId,
    agent_name: agentName,
    destination_dir: destDir,
    file_count: observedFileCount,
    observed_hashes: observedHashes,
    schema_issues: schemaCheck.issues,
    verified_at: new Date().toISOString()
  };
  if (schemaCheck.skipped) result.schema_check_skipped = schemaCheck.skipped;
  if (!schemaCheck.valid) {
    result.error = 'schema_validation_failed';
    result.message = 'Staged openclaw.json violates the installed OpenClaw schema. Fix schema_issues and re-verify before register.';
  }
  return result;
}

function loadManifest(agentName) {
  const agentsRoot = process.env.ISOL8_AGENTS_DIR || path.join(os.homedir(), '.openclaw', 'agents');
  const manifestPath = path.join(agentsRoot, agentName, 'build-manifest.json');
  if (!fs.existsSync(manifestPath)) return null;
  try {
    return JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  } catch (e) {
    return null;
  }
}

function main() {
  const args = parseArgs(process.argv);
  const stdinData = readStdin();

  if (args.action === 'load-manifest') {
    process.stdout.write(JSON.stringify(loadManifest(args.agentName) || {}));
    return;
  }
  if (args.action === 'write-disk') {
    // stdin carries the draft output; pass its artifacts_to_stage so write-disk
    // materializes them into the staging dir before the atomic mv.
    const artifacts = stdinData && stdinData.artifacts_to_stage ? stdinData.artifacts_to_stage : null;
    process.stdout.write(JSON.stringify(writeDisk(args.buildId, args.agentName, artifacts)));
    return;
  }
  if (args.action === 'verify-write') {
    process.stdout.write(JSON.stringify(verifyWrite(args.buildId, args.agentName)));
    return;
  }
  // Default: draft (Step 2 staging)
  process.stdout.write(JSON.stringify(draftToFastIo(stdinData)));
}

if (require.main === module) main();

module.exports = {
  UNIVERSAL_SKILL_CORE,
  renderUserStory,
  generateOpenclawJson,
  generateAgentsMd,
  generateSoulMd,
  generateIdentityMd,
  generateToolsMd,
  generateBootstrapMd,
  generateUserMd,
  generateLobsterPipeline,
  generateScriptArchetype,
  draftToFastIo,
  writeDisk,
  validateStagedConfig,
  verifyWrite,
  loadManifest,
  _flattenTriggers,
  _buildManifest,
  _buildTriggersJson
};
