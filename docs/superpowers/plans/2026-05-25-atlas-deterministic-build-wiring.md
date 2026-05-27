# Atlas Deterministic Build Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Atlas-built agents deterministic at runtime — every requirement classified `deterministic` is bound to a real cron job or hook, files land where `openclaw.json#agents.defaults.workspace` declares, and a hard-block gate (S9.5) refuses to ship agents that claim determinism without a binding.

**Architecture:** Five surgical changes in `agents/atlas/`. (1) Add `triggers[]` to the requirement-matrix schema, plumbed through architect-proposal → vet → recap → build. (2) Make the skeleton generator's `writeDisk` recursive and rooted at `workspace/`. (3) New build step `register-triggers` writes cron jobs to `~/.openclaw/cron/jobs.json` directly (no CLI — that's the persisted shape `catalog_slice.py:204-228` and `config_patcher.append_cron_jobs` already use); hooks become a no-op assert (framework-routed, not per-route registered); channels are dropped from v1. (4) Rollback widens to deregister triggers on register-triggers failure. (5) S9.5 hard gate enforces "deterministic ⇒ triggers.length > 0 AND every pipeline exists in staging."

**Tech Stack:** Node 22 (built-in `node:test`, `node --test` runner). Pure JS — no new dependencies. All scripts use `module.exports` + CLI `main()` pattern matching existing Atlas scripts.

---

## Resolutions of Spec Open Questions

| Q | Resolution | Source |
|---|---|---|
| Q1 — Hook CLI shape | **No CLI.** Hooks are framework-routed via `gateway.hooks` block (`enabled`, `path: "/hooks"`, `allowedSessionKeyPrefixes`). The `register-triggers` `hook` branch becomes a **no-op assert** that verifies the declared `pipeline` file exists in the staged workspace. | `apps/backend/core/containers/config.py:689-723` (no per-route registration anywhere); shipped agents (`agents/echo`, `agents/vera`) declare only `hooks.enabled/path` and nothing per-route. |
| Q2 — Channel binding | **Drop `channel` kind from v1 triggers[].** Shipped agents only carry `channels.{provider}.enabled: true`; there is no agent-scoped `match`/`route` field anywhere in the codebase. Channel routing is global and lives outside Atlas's wiring contract. | `agents/echo/openclaw.json:157-161`, `agents/vera/openclaw.json:184-188`, `apps/backend/core/containers/config.py:738-785` — no `match`/`route` keys. |
| Q3 — Cron job-id naming | **`{build_id}-{req_id}-{n}` as `name`; UUID for the persisted `id` field.** Collision-free across rebuilds (build_id is unique). Rollback uses the `name` prefix `{build_id}-` for cheap deletion. No pre-write delete-sweep needed. | `apps/backend/core/services/catalog_slice.py:208` — persisted `id` is UUID, `name` is human-readable label. |
| Q4 — Cron registration mechanism | **Direct JSON file write to `{cron_store}/jobs.json`** matching the persisted shape `{version: 1, jobs: [{id, agentId, name, enabled, schedule:{kind,expr,tz}, payload:{kind,message}, sessionTarget, wakeMode, delivery, createdAtMs}]}`. The `openclaw cron add` CLI shown in shipped `BOOTSTRAP.md` files is documentation/aspirational — the actual marketplace deploy path (`config_patcher.append_cron_jobs`) writes the JSON directly under `fcntl.lockf`. Atlas-built agents declare `cron.store: "~/.openclaw/cron/jobs.json"` (per `atlas-skeleton-generator.js:101`) — Atlas writes there. | `apps/backend/core/services/catalog_slice.py:204-228` (persisted shape); `apps/backend/core/services/config_patcher.py:591-668` (write mechanism). |

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `agents/atlas/atlas-skeleton-generator.js` | **modify** | (1) `draftToFastIo` consumes per-requirement `triggers[]` from input spec and emits flattened `triggers[]` into the manifest. (2) Template walker emits files under a `workspace/` root subdir. (3) `writeDisk` recursive walk + hash. (4) New `emitManifest` action writes `build-manifest.json` + `triggers.json` to staging. (5) `verifyWrite` recursive. |
| `agents/atlas/atlas-trigger-registrar.js` | **new** | Reads `triggers.json` from the deployed agent dir, writes cron rows into `~/.openclaw/cron/jobs.json` (atomic temp+rename), asserts hook pipelines exist, emits per-trigger result lines + a summary JSON. Supports `--mode=register` and `--mode=deregister`. |
| `agents/atlas/atlas-determinism-audit.js` | **new** | Reads requirement matrix + decisions from fast-io for `build_id`. For every row where final classification is `deterministic`, asserts `triggers.length > 0` AND every trigger's `pipeline` (if non-null) exists in the staging dir. Returns `{pass, failures[]}` so the gate can refuse the build. |
| `agents/atlas/build.lobster` | **modify** | Add `register-triggers` step after `openclaw-agents-add`. Widen `rollback` condition. Add `deregister-triggers` step. Thread per-trigger result into `audit-log`. |
| `agents/atlas/recap-and-cost.lobster` | **modify** | Insert S9.5 `determinism-audit` step between `architecture-signoff` (S9) and the implicit handoff to `build.lobster`. Hard block; no override. |
| `agents/atlas/architect-proposal.lobster` | **modify** | Step 3.6 `archetype-synthesis` prompt + JSON schema gains a `triggers[]` field per requirement. Step 3.7 `snapshot-matrix` persists it. |
| `agents/atlas/vet-walk.lobster` | **modify** | Step 4.4 `present-row` includes the row's `triggers[]` in the Slack message. Step 4.5 `record-decision` persists optional trigger edits. |
| `agents/atlas/atlas-requirement-walker.js` | **modify** | `presentNext` returns `triggers` on the row. New `--action=apply-trigger-edits` accepts user edits and merges into the per-requirement decision. |
| `agents/atlas/AGENTS.md` | **modify** | Append "Determinism Contract" section before the final "Audit Trail" section. Do NOT touch the "Build Session Learnings" appendix. |
| `agents/atlas/tests/triggers-spec.test.js` | **new** | Node test for `triggers[]` schema validation + happy-path skeleton-generator emit. |
| `agents/atlas/tests/recursive-write.test.js` | **new** | Node test for recursive `writeDisk` walking subdirectories. |
| `agents/atlas/tests/trigger-registrar.test.js` | **new** | Node test for cron-job JSON write to a tmpdir cron store. |
| `agents/atlas/tests/determinism-audit.test.js` | **new** | Node test for the audit gate's pass/fail decisions. |

---

## Conventions Reused Across Tasks

**Per-requirement `triggers[]` schema** (added to the existing `archetype-synthesis` output and threaded everywhere):

```json
{
  "req_id": "R7",
  "classification": "deterministic",
  "user_story": "As echo, I should ... so that ...",
  "triggers": [
    { "kind": "cron",       "schedule": "0 8 * * 1", "tz": "America/New_York", "pipeline": "weekly-digest.lobster" },
    { "kind": "hook",       "route": "/hooks/transcript-ready",                 "pipeline": "meeting-process.lobster" },
    { "kind": "agent-call", "callable_as": "agent.invoke('echo', ...)",         "pipeline": null }
  ]
}
```

Field rules (enforced by `atlas-determinism-audit.js`):
- `kind` ∈ `{cron, hook, agent-call}` (channel dropped per Q2).
- `cron`: requires `schedule` (5-field cron expr), optional `tz` (IANA string), `pipeline` MUST be non-null.
- `hook`: requires `route` (string starting with `/hooks/`), `pipeline` MUST be non-null.
- `agent-call`: requires `callable_as` (informational string), `pipeline` MAY be null.
- A requirement with `classification == "deterministic"` MUST have `triggers.length > 0`.

**Cron job-id naming (Q3 resolution):**
- `name` field = `"${build_id}-${req_id}-${n}"` (n = 1-indexed per req).
- `id` field = freshly generated UUID v4 (uses `crypto.randomUUID()`).

**Persisted cron-job shape** (matches `catalog_slice.py:204-228`):

```json
{
  "id": "<uuid>",
  "agentId": "<agent_name>",
  "name": "<build_id>-<req_id>-<n>",
  "enabled": true,
  "schedule": { "kind": "cron", "expr": "<schedule>", "tz": "<tz>" },
  "sessionTarget": "isolated",
  "wakeMode": "light-context",
  "payload": { "kind": "lobster", "message": "Run <pipeline>" },
  "delivery": { "mode": "internal" },
  "createdAtMs": <epoch ms>
}
```

**Cron store path:** `~/.openclaw/cron/jobs.json` for the local-host / Atlas-build target. Override via env `ATLAS_CRON_STORE=<path>` for tests. The path matches what Atlas's generated `openclaw.json` declares at `cron.store` (`atlas-skeleton-generator.js:101`).

**`triggers.json` in the built workspace** at `~/.openclaw/agents/{name}/workspace/triggers.json`:

```json
{
  "agent_name": "echo",
  "build_id": "build_2026-05-25_abc123",
  "triggers": [
    { "req_id": "R7", "kind": "cron", "schedule": "0 8 * * 1", "tz": "America/New_York",
      "pipeline": "weekly-digest.lobster", "registered_as": "build_2026-05-25_abc123-R7-1" }
  ]
}
```

**`build-manifest.json` in staging** at `{staging_dir}/build-manifest.json`:

```json
{
  "build_id": "...",
  "agent_name": "...",
  "generated_at": "<iso>",
  "files": [{"path": "workspace/AGENTS.md", "sha256": "..."}],
  "triggers": [<same shape as triggers.json above, minus registered_as>]
}
```

**Test runner:** Node 22 built-in `node:test` + `node:assert/strict`. Run with `node --test agents/atlas/tests/<file>`.

---

## Task 1: Add `triggers[]` schema doc + acceptance test

**Files:**
- Create: `agents/atlas/tests/triggers-spec.test.js`

The schema lives in code (`atlas-skeleton-generator.js` consumes it) — this test pins the shape so later tasks can't drift.

- [ ] **Step 1: Create the failing test**

Write `agents/atlas/tests/triggers-spec.test.js`:

```javascript
// Pins the per-requirement triggers[] schema consumed by the skeleton
// generator and audited by atlas-determinism-audit.js.
// The schema is enforced in code (no JSON Schema file) — this test is the
// canonical reference for the shape.

const test = require('node:test');
const assert = require('node:assert/strict');

const VALID_KINDS = new Set(['cron', 'hook', 'agent-call']);

function validateTrigger(t) {
  if (!t || typeof t !== 'object') return { ok: false, error: 'trigger must be an object' };
  if (!VALID_KINDS.has(t.kind)) return { ok: false, error: `kind must be one of cron|hook|agent-call (got ${t.kind})` };
  if (t.kind === 'cron') {
    if (typeof t.schedule !== 'string' || t.schedule.trim() === '') return { ok: false, error: 'cron trigger requires non-empty schedule' };
    if (typeof t.pipeline !== 'string' || !t.pipeline.endsWith('.lobster')) return { ok: false, error: 'cron trigger requires .lobster pipeline' };
  } else if (t.kind === 'hook') {
    if (typeof t.route !== 'string' || !t.route.startsWith('/hooks/')) return { ok: false, error: 'hook trigger requires route starting with /hooks/' };
    if (typeof t.pipeline !== 'string' || !t.pipeline.endsWith('.lobster')) return { ok: false, error: 'hook trigger requires .lobster pipeline' };
  } else if (t.kind === 'agent-call') {
    if (typeof t.callable_as !== 'string') return { ok: false, error: 'agent-call trigger requires callable_as string' };
    if (t.pipeline !== null && (typeof t.pipeline !== 'string' || !t.pipeline.endsWith('.lobster'))) return { ok: false, error: 'agent-call pipeline must be null or .lobster string' };
  }
  return { ok: true };
}

function validateRequirementRow(row) {
  const triggers = row.triggers || [];
  if (row.classification === 'deterministic' && triggers.length === 0) {
    return { ok: false, error: `deterministic requirement ${row.req_id} has no triggers` };
  }
  for (const t of triggers) {
    const r = validateTrigger(t);
    if (!r.ok) return { ok: false, error: `${row.req_id}: ${r.error}` };
  }
  return { ok: true };
}

test('cron trigger requires schedule + .lobster pipeline', () => {
  assert.ok(validateTrigger({ kind: 'cron', schedule: '0 8 * * 1', pipeline: 'weekly.lobster' }).ok);
  assert.ok(!validateTrigger({ kind: 'cron', schedule: '0 8 * * 1', pipeline: 'weekly.js' }).ok);
  assert.ok(!validateTrigger({ kind: 'cron', pipeline: 'weekly.lobster' }).ok);
});

test('hook trigger requires /hooks/ route + .lobster pipeline', () => {
  assert.ok(validateTrigger({ kind: 'hook', route: '/hooks/foo', pipeline: 'x.lobster' }).ok);
  assert.ok(!validateTrigger({ kind: 'hook', route: '/foo', pipeline: 'x.lobster' }).ok);
});

test('agent-call may have null pipeline', () => {
  assert.ok(validateTrigger({ kind: 'agent-call', callable_as: 'agent.invoke(...)', pipeline: null }).ok);
  assert.ok(validateTrigger({ kind: 'agent-call', callable_as: 'agent.invoke(...)', pipeline: 'p.lobster' }).ok);
});

test('channel kind is rejected in v1', () => {
  assert.ok(!validateTrigger({ kind: 'channel', channel: 'slack', match: '@echo', pipeline: 'x.lobster' }).ok);
});

test('deterministic requirement with no triggers fails', () => {
  assert.ok(!validateRequirementRow({ req_id: 'R1', classification: 'deterministic', triggers: [] }).ok);
});

test('llm requirement may have zero triggers', () => {
  assert.ok(validateRequirementRow({ req_id: 'R2', classification: 'llm', triggers: [] }).ok);
});

module.exports = { validateTrigger, validateRequirementRow, VALID_KINDS };
```

- [ ] **Step 2: Run the test to verify it passes (test is self-contained)**

Run: `node --test agents/atlas/tests/triggers-spec.test.js`
Expected: all 6 tests pass.

- [ ] **Step 3: Commit**

```bash
git add agents/atlas/tests/triggers-spec.test.js
git commit -m "test(atlas): pin triggers[] schema for deterministic build wiring"
```

---

## Task 2: Skeleton generator — accept `triggers[]` on the spec and emit them into artifacts

**Files:**
- Modify: `agents/atlas/atlas-skeleton-generator.js`

- [ ] **Step 1: Write the failing test**

Append to `agents/atlas/tests/triggers-spec.test.js` (after the existing tests, before `module.exports`):

```javascript
test('draftToFastIo emits triggers.json under workspace/ root', () => {
  const { draftToFastIo } = require('../atlas-skeleton-generator.js');
  const result = draftToFastIo({
    intake: { name: 'testbot', archetype: 'general' },
    user_stories: [
      { req_id: 'R1', agent_name: 'testbot', action: 'do thing', outcome: 'thing is done' }
    ],
    never_does: ['never break things'],
    skills_required: [],
    pipelines: [{ name: 'do-thing', purpose: 'does thing' }],
    requirements_with_triggers: [
      { req_id: 'R1', classification: 'deterministic',
        triggers: [{ kind: 'cron', schedule: '0 8 * * 1', pipeline: 'do-thing.lobster' }] }
    ],
    build_id: 'build_test'
  });
  assert.ok(result.pass, JSON.stringify(result.errors));
  assert.ok(result.artifacts_to_stage['workspace/triggers.json'], 'triggers.json must be under workspace/');
  const parsed = JSON.parse(result.artifacts_to_stage['workspace/triggers.json']);
  assert.equal(parsed.agent_name, 'testbot');
  assert.equal(parsed.build_id, 'build_test');
  assert.equal(parsed.triggers.length, 1);
  assert.equal(parsed.triggers[0].req_id, 'R1');
  assert.equal(parsed.triggers[0].kind, 'cron');
});

test('draftToFastIo emits build-manifest.json with files[] and triggers[]', () => {
  const { draftToFastIo } = require('../atlas-skeleton-generator.js');
  const result = draftToFastIo({
    intake: { name: 'testbot' },
    user_stories: [{ req_id: 'R1', agent_name: 'testbot', action: 'x', outcome: 'y' }],
    never_does: ['nope'],
    requirements_with_triggers: [],
    build_id: 'build_test'
  });
  assert.ok(result.pass);
  assert.ok(result.artifacts_to_stage['build-manifest.json']);
  const m = JSON.parse(result.artifacts_to_stage['build-manifest.json']);
  assert.equal(m.build_id, 'build_test');
  assert.equal(m.agent_name, 'testbot');
  assert.ok(Array.isArray(m.files));
  assert.ok(Array.isArray(m.triggers));
});

test('draftToFastIo places ALL generated agent files under workspace/', () => {
  const { draftToFastIo } = require('../atlas-skeleton-generator.js');
  const result = draftToFastIo({
    intake: { name: 'testbot' },
    user_stories: [{ req_id: 'R1', agent_name: 'testbot', action: 'x', outcome: 'y' }],
    never_does: ['nope'],
    pipelines: [{ name: 'p1', purpose: 'pp' }],
    scripts: [{ name: 's1', purpose: 'ss' }],
    requirements_with_triggers: [],
    build_id: 'build_test'
  });
  assert.ok(result.pass);
  for (const name of ['AGENTS.md', 'SOUL.md', 'IDENTITY.md', 'TOOLS.md', 'USER.md',
                      'BOOTSTRAP.md', 'openclaw.json', 'p1.lobster', 's1.js']) {
    assert.ok(result.artifacts_to_stage[`workspace/${name}`],
      `${name} must be under workspace/ (got keys: ${Object.keys(result.artifacts_to_stage).join(',')})`);
  }
  // build-manifest.json is the ONLY artifact at the staging root.
  assert.ok(result.artifacts_to_stage['build-manifest.json']);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test agents/atlas/tests/triggers-spec.test.js`
Expected: 3 new tests fail — `result.artifacts_to_stage['workspace/triggers.json']` is undefined; today's generator places files at staging root with no `workspace/` prefix and emits no `triggers.json` / `build-manifest.json`.

- [ ] **Step 3: Modify `draftToFastIo` to emit under `workspace/` + add manifest + triggers.json**

Edit `agents/atlas/atlas-skeleton-generator.js`. Replace the `draftToFastIo` function (lines 399-441) with:

```javascript
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
    underWorkspace[`workspace/${pipeline.name}.lobster`] = generateLobsterPipeline(pipeline);
  }
  for (const script of (spec.scripts || [])) {
    underWorkspace[`workspace/${script.name}.js`] = generateScriptArchetype(script);
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
```

Also update `module.exports` at the bottom (lines 600-615) to add the helpers:

```javascript
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
  verifyWrite,
  _flattenTriggers,
  _buildManifest,
  _buildTriggersJson
};
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `node --test agents/atlas/tests/triggers-spec.test.js`
Expected: all 9 tests pass.

- [ ] **Step 5: Commit**

```bash
git add agents/atlas/atlas-skeleton-generator.js agents/atlas/tests/triggers-spec.test.js
git commit -m "feat(atlas): emit triggers.json + build-manifest.json under workspace/ root"
```

---

## Task 3: Skeleton generator — recursive writeDisk + verifyWrite

**Files:**
- Create: `agents/atlas/tests/recursive-write.test.js`
- Modify: `agents/atlas/atlas-skeleton-generator.js` (functions `writeDisk` and `verifyWrite`)

- [ ] **Step 1: Write the failing test**

Create `agents/atlas/tests/recursive-write.test.js`:

```javascript
// writeDisk must recursively walk staging-dir subdirectories.
// Today's implementation reads only files at the staging root and drops
// any nested directories — workspace/ would be silently lost.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

function makeTmpAgentsRoot() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'atlas-test-agents-'));
}

function seedStaging(agentsRoot, buildId, files) {
  const staging = path.join(agentsRoot, `.atlas-staging-${buildId}`);
  fs.mkdirSync(staging);
  for (const [relPath, content] of Object.entries(files)) {
    const full = path.join(staging, relPath);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, content);
  }
  return staging;
}

test('writeDisk preserves nested workspace/ subdirectory', () => {
  const agentsRoot = makeTmpAgentsRoot();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  const buildId = 'build_recursive_1';
  seedStaging(agentsRoot, buildId, {
    'build-manifest.json': JSON.stringify({ build_id: buildId, agent_name: 'rebot' }),
    'workspace/openclaw.json': JSON.stringify({ agents: { list: [{ id: 'rebot' }] } }),
    'workspace/AGENTS.md': '# AGENTS\n',
    'workspace/skills/foo/SKILL.md': '# foo\n',
    'workspace/p.lobster': 'name: p\n'
  });
  const { writeDisk } = require('../atlas-skeleton-generator.js');
  const result = writeDisk(buildId, 'rebot');
  assert.ok(result.success, JSON.stringify(result));
  // Nested files preserved.
  assert.ok(fs.existsSync(path.join(agentsRoot, 'rebot', 'workspace', 'openclaw.json')));
  assert.ok(fs.existsSync(path.join(agentsRoot, 'rebot', 'workspace', 'AGENTS.md')));
  assert.ok(fs.existsSync(path.join(agentsRoot, 'rebot', 'workspace', 'skills', 'foo', 'SKILL.md')));
  assert.ok(fs.existsSync(path.join(agentsRoot, 'rebot', 'workspace', 'p.lobster')));
  // build-manifest.json at root.
  assert.ok(fs.existsSync(path.join(agentsRoot, 'rebot', 'build-manifest.json')));
  // file_hashes contains every nested file (relative paths).
  assert.ok(result.file_hashes['workspace/skills/foo/SKILL.md']);
  assert.equal(result.file_count, 5);
});

test('verifyWrite recursively hashes nested files', () => {
  const agentsRoot = makeTmpAgentsRoot();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  const destDir = path.join(agentsRoot, 'vbot');
  fs.mkdirSync(path.join(destDir, 'workspace', 'inner'), { recursive: true });
  fs.writeFileSync(path.join(destDir, 'build-manifest.json'), 'm');
  fs.writeFileSync(path.join(destDir, 'workspace', 'a.md'), 'a');
  fs.writeFileSync(path.join(destDir, 'workspace', 'inner', 'b.md'), 'b');

  const { verifyWrite } = require('../atlas-skeleton-generator.js');
  const result = verifyWrite('build_v_1', 'vbot');
  assert.ok(result.success);
  assert.equal(result.file_count, 3);
  assert.ok(result.observed_hashes['workspace/a.md']);
  assert.ok(result.observed_hashes['workspace/inner/b.md']);
  assert.ok(result.observed_hashes['build-manifest.json']);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test agents/atlas/tests/recursive-write.test.js`
Expected: both tests fail — `writeDisk` reads only `isFile()` entries at the staging root (line 484), nested files are dropped during `fs.renameSync` of the staging dir (rename moves the whole tree, so they ARE on disk — BUT the hash table only includes root files, AND `verifyWrite` walks only the root, so the hash table mismatches and `file_count` is wrong).

- [ ] **Step 3: Modify `writeDisk` to walk recursively**

Edit `agents/atlas/atlas-skeleton-generator.js`. Replace the staging-files-collection block (lines 482-490) inside `writeDisk` with:

```javascript
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
```

Then in the `success: true` return block (around line 511-521) replace `file_count: stagedFiles.length` with `file_count: stagedFileCount` and confirm `file_hashes: stagedHashes` is the recursive map.

- [ ] **Step 4: Modify `verifyWrite` to walk recursively**

In `verifyWrite` (lines 551-580), replace the file-collection block:

```javascript
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
```

Then update the return block's `file_count: files.length` to `file_count: observedFileCount`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `node --test agents/atlas/tests/recursive-write.test.js`
Expected: both tests pass.

Run the existing test too: `node --test agents/atlas/tests/triggers-spec.test.js`
Expected: still 9 passing.

- [ ] **Step 6: Commit**

```bash
git add agents/atlas/atlas-skeleton-generator.js agents/atlas/tests/recursive-write.test.js
git commit -m "feat(atlas): writeDisk + verifyWrite walk staging tree recursively"
```

---

## Task 4: Skeleton generator — `--action=load-manifest` accessor for the registrar

**Files:**
- Modify: `agents/atlas/atlas-skeleton-generator.js` (`main` function adds new action; also export a `loadManifest` helper).

The registrar (next task) needs to read the deployed agent's `triggers.json`. Expose a small helper so we don't reimplement path resolution.

- [ ] **Step 1: Write the failing test**

Append to `agents/atlas/tests/recursive-write.test.js` (before the last bare line if any):

```javascript
test('loadManifest reads build-manifest.json from deployed agent root', () => {
  const agentsRoot = makeTmpAgentsRoot();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  const destDir = path.join(agentsRoot, 'lmbot');
  fs.mkdirSync(destDir, { recursive: true });
  const manifest = { build_id: 'b1', agent_name: 'lmbot', triggers: [{ kind: 'cron', req_id: 'R1' }] };
  fs.writeFileSync(path.join(destDir, 'build-manifest.json'), JSON.stringify(manifest));

  const { loadManifest } = require('../atlas-skeleton-generator.js');
  const loaded = loadManifest('lmbot');
  assert.deepEqual(loaded, manifest);
});

test('loadManifest returns null when manifest missing', () => {
  const agentsRoot = makeTmpAgentsRoot();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  fs.mkdirSync(path.join(agentsRoot, 'nomanibot'), { recursive: true });
  const { loadManifest } = require('../atlas-skeleton-generator.js');
  assert.equal(loadManifest('nomanibot'), null);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test agents/atlas/tests/recursive-write.test.js`
Expected: `loadManifest is not a function` errors on both new tests.

- [ ] **Step 3: Add `loadManifest` to the generator**

In `agents/atlas/atlas-skeleton-generator.js`, add this function just before `function main()` (around line 582):

```javascript
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
```

Add `loadManifest` to the `module.exports` object at the bottom.

Also add a `--action=load-manifest` handler in `main()` (before the `// Default: draft` comment):

```javascript
  if (args.action === 'load-manifest') {
    process.stdout.write(JSON.stringify(loadManifest(args.agentName) || {}));
    return;
  }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test agents/atlas/tests/recursive-write.test.js`
Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add agents/atlas/atlas-skeleton-generator.js agents/atlas/tests/recursive-write.test.js
git commit -m "feat(atlas): loadManifest helper + --action=load-manifest"
```

---

## Task 5: `atlas-trigger-registrar.js` — happy-path cron registration

**Files:**
- Create: `agents/atlas/atlas-trigger-registrar.js`
- Create: `agents/atlas/tests/trigger-registrar.test.js`

- [ ] **Step 1: Write the failing test**

Create `agents/atlas/tests/trigger-registrar.test.js`:

```javascript
// Trigger registrar — writes cron jobs into ~/.openclaw/cron/jobs.json
// (override via ATLAS_CRON_STORE env), asserts hook pipelines exist,
// emits per-trigger result lines + summary JSON.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

function mkTmpDirs() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'atlas-reg-'));
  const agentsRoot = path.join(root, 'agents');
  const cronStore = path.join(root, 'cron', 'jobs.json');
  fs.mkdirSync(agentsRoot, { recursive: true });
  fs.mkdirSync(path.dirname(cronStore), { recursive: true });
  return { agentsRoot, cronStore };
}

function seedDeployedAgent(agentsRoot, agentName, manifest, workspaceFiles = {}) {
  const dest = path.join(agentsRoot, agentName);
  const workspace = path.join(dest, 'workspace');
  fs.mkdirSync(workspace, { recursive: true });
  fs.writeFileSync(path.join(dest, 'build-manifest.json'), JSON.stringify(manifest));
  for (const [rel, content] of Object.entries(workspaceFiles)) {
    fs.writeFileSync(path.join(workspace, rel), content);
  }
  return dest;
}

test('register writes a cron job entry to ATLAS_CRON_STORE', () => {
  const { agentsRoot, cronStore } = mkTmpDirs();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  process.env.ATLAS_CRON_STORE = cronStore;

  seedDeployedAgent(agentsRoot, 'crbot', {
    build_id: 'B1', agent_name: 'crbot',
    triggers: [
      { req_id: 'R1', kind: 'cron', schedule: '0 8 * * 1', tz: 'America/New_York', pipeline: 'weekly.lobster' }
    ]
  }, { 'weekly.lobster': 'name: weekly\n' });

  const { register } = require('../atlas-trigger-registrar.js');
  const result = register({ buildId: 'B1', agentName: 'crbot' });
  assert.ok(result.success, JSON.stringify(result));
  assert.equal(result.registered.length, 1);
  assert.equal(result.registered[0].name, 'B1-R1-1');
  assert.equal(result.registered[0].kind, 'cron');

  const store = JSON.parse(fs.readFileSync(cronStore, 'utf8'));
  assert.equal(store.version, 1);
  assert.equal(store.jobs.length, 1);
  const job = store.jobs[0];
  assert.equal(job.name, 'B1-R1-1');
  assert.equal(job.agentId, 'crbot');
  assert.equal(job.schedule.expr, '0 8 * * 1');
  assert.equal(job.schedule.tz, 'America/New_York');
  assert.equal(job.schedule.kind, 'cron');
  assert.equal(job.payload.kind, 'lobster');
  assert.equal(job.enabled, true);
  assert.ok(typeof job.id === 'string' && job.id.length > 0);
  assert.ok(typeof job.createdAtMs === 'number');
});

test('register appends to existing jobs.json without clobbering', () => {
  const { agentsRoot, cronStore } = mkTmpDirs();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  process.env.ATLAS_CRON_STORE = cronStore;
  fs.writeFileSync(cronStore, JSON.stringify({ version: 1, jobs: [{ id: 'pre', name: 'pre-existing' }] }));

  seedDeployedAgent(agentsRoot, 'crbot', {
    build_id: 'B2', agent_name: 'crbot',
    triggers: [{ req_id: 'R1', kind: 'cron', schedule: '* * * * *', pipeline: 'p.lobster' }]
  }, { 'p.lobster': 'name: p\n' });

  const { register } = require('../atlas-trigger-registrar.js');
  const result = register({ buildId: 'B2', agentName: 'crbot' });
  assert.ok(result.success);

  const store = JSON.parse(fs.readFileSync(cronStore, 'utf8'));
  assert.equal(store.jobs.length, 2);
  assert.equal(store.jobs[0].id, 'pre');  // pre-existing preserved
  assert.equal(store.jobs[1].name, 'B2-R1-1');
});

test('register updates triggers.json with registered_as', () => {
  const { agentsRoot, cronStore } = mkTmpDirs();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  process.env.ATLAS_CRON_STORE = cronStore;
  const dest = seedDeployedAgent(agentsRoot, 'crbot', {
    build_id: 'B3', agent_name: 'crbot',
    triggers: [{ req_id: 'R1', kind: 'cron', schedule: '* * * * *', pipeline: 'p.lobster' }]
  }, { 'p.lobster': 'name: p\n' });
  // Seed an empty triggers.json under workspace/ so the registrar can update it.
  fs.writeFileSync(
    path.join(dest, 'workspace', 'triggers.json'),
    JSON.stringify({ agent_name: 'crbot', build_id: 'B3', triggers: [
      { req_id: 'R1', kind: 'cron', schedule: '* * * * *', pipeline: 'p.lobster', registered_as: null }
    ]})
  );

  const { register } = require('../atlas-trigger-registrar.js');
  const result = register({ buildId: 'B3', agentName: 'crbot' });
  assert.ok(result.success);

  const tj = JSON.parse(fs.readFileSync(path.join(dest, 'workspace', 'triggers.json'), 'utf8'));
  assert.equal(tj.triggers[0].registered_as, 'B3-R1-1');
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test agents/atlas/tests/trigger-registrar.test.js`
Expected: all 3 tests fail — module doesn't exist yet.

- [ ] **Step 3: Create the registrar**

Create `agents/atlas/atlas-trigger-registrar.js`:

```javascript
#!/usr/bin/env node
// atlas-trigger-registrar.js
//
// Registers triggers for a freshly-built Atlas agent. Two modes:
//   --mode=register   — write cron rows + assert hook pipelines exist
//   --mode=deregister — remove every cron row whose name starts with {build_id}-
//
// Cron mechanism (per spec Q4 resolution): direct JSON write to the
// configured cron store. Matches the persisted shape in
// catalog_slice.py:204-228 and the write mechanism in
// config_patcher.append_cron_jobs.
//
// Hook mechanism (per spec Q1 resolution): no per-route CLI exists; hooks
// are framework-routed by session-key prefix. This registrar's hook branch
// is a no-op assert — it verifies the trigger's declared .lobster pipeline
// is present in the deployed agent's workspace/ dir.
//
// Channel kind is NOT supported in v1 (spec Q2) — flagged as an error.
//
// Cron-job naming (per spec Q3): name = `{build_id}-{req_id}-{n}` (1-indexed
// across the build's triggers list for that req_id); id is a fresh UUID v4.

const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

function parseArgs(argv) {
  const args = { mode: 'register', buildId: null, agentName: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--mode=')) args.mode = a.split('=')[1];
    else if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a.startsWith('--agent-name=')) args.agentName = a.split('=')[1];
  }
  return args;
}

function _agentsRoot() {
  return process.env.ISOL8_AGENTS_DIR || path.join(os.homedir(), '.openclaw', 'agents');
}

function _cronStorePath() {
  return process.env.ATLAS_CRON_STORE || path.join(os.homedir(), '.openclaw', 'cron', 'jobs.json');
}

function _loadCronStore() {
  const p = _cronStorePath();
  if (!fs.existsSync(p)) return { version: 1, jobs: [] };
  try {
    const data = JSON.parse(fs.readFileSync(p, 'utf8'));
    if (!data || typeof data !== 'object') return { version: 1, jobs: [] };
    if (!Array.isArray(data.jobs)) data.jobs = [];
    if (!data.version) data.version = 1;
    return data;
  } catch (e) {
    return { version: 1, jobs: [] };
  }
}

function _writeCronStore(store) {
  const p = _cronStorePath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  // Atomic temp+rename to avoid corruption on crash mid-write.
  const tmp = p + '.tmp-' + process.pid + '-' + Date.now();
  fs.writeFileSync(tmp, JSON.stringify(store, null, 2));
  fs.renameSync(tmp, p);
}

function _buildCronJob({ buildId, reqId, n, agentName, schedule, tz, pipeline }) {
  const name = `${buildId}-${reqId}-${n}`;
  return {
    id: crypto.randomUUID(),
    agentId: agentName,
    name,
    enabled: true,
    schedule: { kind: 'cron', expr: schedule, tz: tz || null },
    sessionTarget: 'isolated',
    wakeMode: 'light-context',
    payload: { kind: 'lobster', message: `Run ${pipeline}` },
    delivery: { mode: 'internal' },
    createdAtMs: Date.now()
  };
}

function _loadManifest(agentName) {
  const manifestPath = path.join(_agentsRoot(), agentName, 'build-manifest.json');
  if (!fs.existsSync(manifestPath)) return null;
  try { return JSON.parse(fs.readFileSync(manifestPath, 'utf8')); } catch (e) { return null; }
}

function _workspaceHasFile(agentName, relPath) {
  const full = path.join(_agentsRoot(), agentName, 'workspace', relPath);
  return fs.existsSync(full);
}

function _updateTriggersJson(agentName, registeredEntries) {
  // registeredEntries: [{req_id, kind, registered_as}]
  const triggersPath = path.join(_agentsRoot(), agentName, 'workspace', 'triggers.json');
  if (!fs.existsSync(triggersPath)) return;
  let data;
  try { data = JSON.parse(fs.readFileSync(triggersPath, 'utf8')); } catch (e) { return; }
  if (!Array.isArray(data.triggers)) return;
  // Match by (req_id, kind) — there can be multiple triggers per req of the
  // same kind, so consume entries in order.
  const queue = registeredEntries.slice();
  for (const t of data.triggers) {
    const idx = queue.findIndex(r => r.req_id === t.req_id && r.kind === t.kind);
    if (idx === -1) continue;
    t.registered_as = queue[idx].registered_as;
    queue.splice(idx, 1);
  }
  fs.writeFileSync(triggersPath, JSON.stringify(data, null, 2));
}

function register({ buildId, agentName }) {
  if (!buildId || !agentName) {
    return { success: false, error: 'build_id_or_agent_name_missing', registered: [] };
  }
  const manifest = _loadManifest(agentName);
  if (!manifest) {
    return { success: false, error: 'manifest_not_found', agent_name: agentName, registered: [] };
  }
  const triggers = Array.isArray(manifest.triggers) ? manifest.triggers : [];

  const registered = [];
  const errors = [];

  // Per-req_id 1-indexed counter for the name suffix.
  const perReqCounter = {};

  for (const t of triggers) {
    const reqId = t.req_id;
    perReqCounter[reqId] = (perReqCounter[reqId] || 0) + 1;
    const n = perReqCounter[reqId];

    if (t.kind === 'cron') {
      if (!t.schedule || !t.pipeline) {
        errors.push({ req_id: reqId, kind: 'cron', error: 'missing schedule or pipeline' });
        break;
      }
      if (!_workspaceHasFile(agentName, t.pipeline)) {
        errors.push({ req_id: reqId, kind: 'cron', error: `pipeline file not in workspace: ${t.pipeline}` });
        break;
      }
      const job = _buildCronJob({
        buildId, reqId, n, agentName,
        schedule: t.schedule, tz: t.tz, pipeline: t.pipeline
      });
      const store = _loadCronStore();
      store.jobs.push(job);
      _writeCronStore(store);
      registered.push({ req_id: reqId, kind: 'cron', registered_as: job.name, job_id: job.id });
    } else if (t.kind === 'hook') {
      // No CLI to call — hooks are framework-routed. Just assert the pipeline exists.
      if (!t.pipeline || !t.pipeline.endsWith('.lobster')) {
        errors.push({ req_id: reqId, kind: 'hook', error: 'hook requires .lobster pipeline' });
        break;
      }
      if (!_workspaceHasFile(agentName, t.pipeline)) {
        errors.push({ req_id: reqId, kind: 'hook', error: `pipeline file not in workspace: ${t.pipeline}` });
        break;
      }
      registered.push({ req_id: reqId, kind: 'hook', registered_as: `framework-routed:${t.route}` });
    } else if (t.kind === 'agent-call') {
      // No registration needed. If pipeline is non-null, assert it exists.
      if (t.pipeline !== null && t.pipeline !== undefined) {
        if (!t.pipeline.endsWith('.lobster')) {
          errors.push({ req_id: reqId, kind: 'agent-call', error: 'agent-call pipeline must be null or .lobster' });
          break;
        }
        if (!_workspaceHasFile(agentName, t.pipeline)) {
          errors.push({ req_id: reqId, kind: 'agent-call', error: `pipeline file not in workspace: ${t.pipeline}` });
          break;
        }
      }
      registered.push({ req_id: reqId, kind: 'agent-call', registered_as: 'no-registration-required' });
    } else {
      errors.push({ req_id: reqId, kind: t.kind, error: `unsupported trigger kind: ${t.kind}` });
      break;
    }
  }

  if (errors.length > 0) {
    return {
      success: false,
      build_id: buildId,
      agent_name: agentName,
      registered,
      errors,
      message: 'register-triggers aborted at first failure; partially-registered entries preserved for rollback'
    };
  }

  _updateTriggersJson(agentName, registered);

  return {
    success: true,
    build_id: buildId,
    agent_name: agentName,
    registered,
    cron_registered_count: registered.filter(r => r.kind === 'cron').length,
    hook_asserted_count: registered.filter(r => r.kind === 'hook').length,
    agent_call_noted_count: registered.filter(r => r.kind === 'agent-call').length
  };
}

function deregister({ buildId, agentName }) {
  if (!buildId || !agentName) {
    return { success: false, error: 'build_id_or_agent_name_missing' };
  }
  const store = _loadCronStore();
  const before = store.jobs.length;
  const namePrefix = `${buildId}-`;
  store.jobs = store.jobs.filter(j => !(j.agentId === agentName && typeof j.name === 'string' && j.name.startsWith(namePrefix)));
  const removed = before - store.jobs.length;
  if (removed > 0) {
    _writeCronStore(store);
  }
  return {
    success: true,
    build_id: buildId,
    agent_name: agentName,
    cron_removed: removed
  };
}

function main() {
  const args = parseArgs(process.argv);
  let result;
  if (args.mode === 'register') {
    result = register({ buildId: args.buildId, agentName: args.agentName });
  } else if (args.mode === 'deregister') {
    result = deregister({ buildId: args.buildId, agentName: args.agentName });
  } else {
    result = { success: false, error: `unknown mode: ${args.mode}` };
  }
  process.stdout.write(JSON.stringify(result));
  if (!result.success) process.exit(1);
}

if (require.main === module) main();

module.exports = { register, deregister, _buildCronJob, _loadCronStore };
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test agents/atlas/tests/trigger-registrar.test.js`
Expected: all 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add agents/atlas/atlas-trigger-registrar.js agents/atlas/tests/trigger-registrar.test.js
git commit -m "feat(atlas): trigger registrar — write cron jobs to ~/.openclaw/cron/jobs.json"
```

---

## Task 6: Trigger registrar — failure-path tests (hook missing pipeline; deregister)

**Files:**
- Modify: `agents/atlas/tests/trigger-registrar.test.js`

- [ ] **Step 1: Append failure-path tests**

Append to `agents/atlas/tests/trigger-registrar.test.js`:

```javascript
test('register fails when hook trigger references missing pipeline', () => {
  const { agentsRoot, cronStore } = mkTmpDirs();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  process.env.ATLAS_CRON_STORE = cronStore;
  seedDeployedAgent(agentsRoot, 'hbot', {
    build_id: 'B1', agent_name: 'hbot',
    triggers: [
      { req_id: 'R1', kind: 'hook', route: '/hooks/x', pipeline: 'missing.lobster' }
    ]
  }, {});  // no workspace files

  const { register } = require('../atlas-trigger-registrar.js');
  const result = register({ buildId: 'B1', agentName: 'hbot' });
  assert.ok(!result.success);
  assert.equal(result.errors[0].kind, 'hook');
  assert.match(result.errors[0].error, /pipeline file not in workspace/);
});

test('register rejects channel kind (v1)', () => {
  const { agentsRoot, cronStore } = mkTmpDirs();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  process.env.ATLAS_CRON_STORE = cronStore;
  seedDeployedAgent(agentsRoot, 'cbot', {
    build_id: 'B1', agent_name: 'cbot',
    triggers: [{ req_id: 'R1', kind: 'channel', channel: 'slack', match: '@x', pipeline: 'x.lobster' }]
  }, { 'x.lobster': 'name: x\n' });

  const { register } = require('../atlas-trigger-registrar.js');
  const result = register({ buildId: 'B1', agentName: 'cbot' });
  assert.ok(!result.success);
  assert.match(result.errors[0].error, /unsupported trigger kind: channel/);
});

test('register aborts at first failure but preserves earlier registrations', () => {
  const { agentsRoot, cronStore } = mkTmpDirs();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  process.env.ATLAS_CRON_STORE = cronStore;
  seedDeployedAgent(agentsRoot, 'pbot', {
    build_id: 'B1', agent_name: 'pbot',
    triggers: [
      { req_id: 'R1', kind: 'cron', schedule: '* * * * *', pipeline: 'ok.lobster' },
      { req_id: 'R2', kind: 'hook',  route: '/hooks/x',    pipeline: 'gone.lobster' }
    ]
  }, { 'ok.lobster': 'name: ok\n' });

  const { register } = require('../atlas-trigger-registrar.js');
  const result = register({ buildId: 'B1', agentName: 'pbot' });
  assert.ok(!result.success);
  // Cron was already written before the hook failure.
  assert.equal(result.registered.length, 1);
  assert.equal(result.registered[0].kind, 'cron');
  // The cron job is in the store and waiting for rollback.
  const store = JSON.parse(fs.readFileSync(cronStore, 'utf8'));
  assert.equal(store.jobs.length, 1);
  assert.equal(store.jobs[0].name, 'B1-R1-1');
});

test('deregister removes only this build_id+agent_name jobs', () => {
  const { agentsRoot, cronStore } = mkTmpDirs();
  process.env.ATLAS_CRON_STORE = cronStore;
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  fs.writeFileSync(cronStore, JSON.stringify({
    version: 1,
    jobs: [
      { id: 'a', agentId: 'pbot', name: 'B1-R1-1' },
      { id: 'b', agentId: 'pbot', name: 'B1-R2-1' },
      { id: 'c', agentId: 'other', name: 'B1-R1-1' },     // different agent — keep
      { id: 'd', agentId: 'pbot', name: 'B2-R1-1' }        // different build — keep
    ]
  }));
  const { deregister } = require('../atlas-trigger-registrar.js');
  const result = deregister({ buildId: 'B1', agentName: 'pbot' });
  assert.ok(result.success);
  assert.equal(result.cron_removed, 2);
  const store = JSON.parse(fs.readFileSync(cronStore, 'utf8'));
  assert.equal(store.jobs.length, 2);
  assert.deepEqual(store.jobs.map(j => j.id).sort(), ['c', 'd']);
});
```

- [ ] **Step 2: Run the tests to verify they pass**

Run: `node --test agents/atlas/tests/trigger-registrar.test.js`
Expected: all 7 tests pass.

- [ ] **Step 3: Commit**

```bash
git add agents/atlas/tests/trigger-registrar.test.js
git commit -m "test(atlas): registrar failure paths + scoped deregister"
```

---

## Task 7: `atlas-determinism-audit.js` — S9.5 hard gate

**Files:**
- Create: `agents/atlas/atlas-determinism-audit.js`
- Create: `agents/atlas/tests/determinism-audit.test.js`

The audit is invoked from `recap-and-cost.lobster` AFTER S9 sign-off and BEFORE handoff to `build.lobster`. It reads the requirement matrix + per-requirement decisions from fast-io (via `openclaw.invoke --tool fast-io`) and asserts every `deterministic` row has `triggers.length > 0` with every pipeline present in the staging dir.

For unit testability, the file-system + fast-io readers are injectable via an `_ioAdapter` argument.

- [ ] **Step 1: Write the failing test**

Create `agents/atlas/tests/determinism-audit.test.js`:

```javascript
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

function mkStaging(buildId, files = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'atlas-audit-'));
  const agentsRoot = path.join(root, 'agents');
  const staging = path.join(agentsRoot, `.atlas-staging-${buildId}`);
  fs.mkdirSync(path.join(staging, 'workspace'), { recursive: true });
  for (const [rel, content] of Object.entries(files)) {
    const full = path.join(staging, 'workspace', rel);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, content);
  }
  return { agentsRoot, stagingDir: staging };
}

function mockIo({ matrix, decisions }) {
  return {
    readMatrix: () => matrix,
    listDecisions: () => decisions
  };
}

test('audit passes when every deterministic req has triggers + pipelines exist', () => {
  const { stagingDir, agentsRoot } = mkStaging('B1', { 'p.lobster': 'name: p\n' });
  process.env.ISOL8_AGENTS_DIR = agentsRoot;

  const matrix = { user_stories: [
    { req_id: 'R1', classification: 'deterministic',
      triggers: [{ kind: 'cron', schedule: '* * * * *', pipeline: 'p.lobster' }] },
    { req_id: 'R2', classification: 'llm', triggers: [] }
  ]};
  const decisions = [
    { req_id: 'R1', decision: 'approve' },
    { req_id: 'R2', decision: 'approve' }
  ];
  const { audit } = require('../atlas-determinism-audit.js');
  const result = audit({ buildId: 'B1', stagingDir, _ioAdapter: mockIo({ matrix, decisions }) });
  assert.ok(result.pass, JSON.stringify(result));
  assert.equal(result.failures.length, 0);
});

test('audit fails when deterministic req has zero triggers', () => {
  const { stagingDir, agentsRoot } = mkStaging('B2');
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  const matrix = { user_stories: [
    { req_id: 'R1', classification: 'deterministic', triggers: [] }
  ]};
  const decisions = [{ req_id: 'R1', decision: 'approve' }];
  const { audit } = require('../atlas-determinism-audit.js');
  const result = audit({ buildId: 'B2', stagingDir, _ioAdapter: mockIo({ matrix, decisions }) });
  assert.ok(!result.pass);
  assert.equal(result.failures.length, 1);
  assert.equal(result.failures[0].req_id, 'R1');
  assert.match(result.failures[0].reason, /no_triggers/);
});

test('audit fails when trigger pipeline missing from staging', () => {
  const { stagingDir, agentsRoot } = mkStaging('B3');  // no p.lobster
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  const matrix = { user_stories: [
    { req_id: 'R1', classification: 'deterministic',
      triggers: [{ kind: 'cron', schedule: '* * * * *', pipeline: 'p.lobster' }] }
  ]};
  const decisions = [{ req_id: 'R1', decision: 'approve' }];
  const { audit } = require('../atlas-determinism-audit.js');
  const result = audit({ buildId: 'B3', stagingDir, _ioAdapter: mockIo({ matrix, decisions }) });
  assert.ok(!result.pass);
  assert.equal(result.failures.length, 1);
  assert.match(result.failures[0].reason, /pipeline_missing/);
  assert.equal(result.failures[0].pipeline, 'p.lobster');
});

test('audit ignores dropped requirements', () => {
  const { stagingDir, agentsRoot } = mkStaging('B4');
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  const matrix = { user_stories: [
    { req_id: 'R1', classification: 'deterministic', triggers: [] }
  ]};
  const decisions = [{ req_id: 'R1', decision: 'drop', user_note: 'not needed' }];
  const { audit } = require('../atlas-determinism-audit.js');
  const result = audit({ buildId: 'B4', stagingDir, _ioAdapter: mockIo({ matrix, decisions }) });
  assert.ok(result.pass, 'dropped reqs must be skipped: ' + JSON.stringify(result));
});

test('audit honors classification overrides in the decision', () => {
  // User changed R1 from deterministic to llm during vet. The audit should
  // respect the decided classification, not the original matrix one.
  const { stagingDir, agentsRoot } = mkStaging('B5');
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  const matrix = { user_stories: [
    { req_id: 'R1', classification: 'deterministic', triggers: [] }
  ]};
  const decisions = [{ req_id: 'R1', decision: 'change', classification_override: 'llm' }];
  const { audit } = require('../atlas-determinism-audit.js');
  const result = audit({ buildId: 'B5', stagingDir, _ioAdapter: mockIo({ matrix, decisions }) });
  assert.ok(result.pass);
});

test('audit fails with multiple failures aggregated', () => {
  const { stagingDir, agentsRoot } = mkStaging('B6');  // no pipelines
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  const matrix = { user_stories: [
    { req_id: 'R1', classification: 'deterministic', triggers: [] },
    { req_id: 'R2', classification: 'deterministic',
      triggers: [{ kind: 'cron', schedule: '* * * * *', pipeline: 'gone.lobster' }] }
  ]};
  const decisions = [
    { req_id: 'R1', decision: 'approve' },
    { req_id: 'R2', decision: 'approve' }
  ];
  const { audit } = require('../atlas-determinism-audit.js');
  const result = audit({ buildId: 'B6', stagingDir, _ioAdapter: mockIo({ matrix, decisions }) });
  assert.ok(!result.pass);
  assert.equal(result.failures.length, 2);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `node --test agents/atlas/tests/determinism-audit.test.js`
Expected: all 6 tests fail — module doesn't exist.

- [ ] **Step 3: Create `atlas-determinism-audit.js`**

Create `agents/atlas/atlas-determinism-audit.js`:

```javascript
#!/usr/bin/env node
// atlas-determinism-audit.js
//
// S9.5 hard gate. After S9 architecture sign-off, before Step 6 build.
// For every requirement whose final classification is `deterministic`:
//   - assert triggers.length > 0
//   - for every trigger with a non-null pipeline, assert the pipeline file
//     exists in the staging dir's workspace/
//
// Final classification = decision.classification_override (if present) else
// the matrix's original classification. Decisions with decision == 'drop'
// are excluded from the audit (a dropped req has no runtime obligation).
//
// No agentgate override. Matches Atlas's existing hard-rule posture
// (ClawHavoc denylist, file-write-gate).

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execSync } = require('child_process');

function parseArgs(argv) {
  const args = { buildId: null, stagingDir: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a.startsWith('--staging-dir=')) args.stagingDir = a.split('=')[1];
  }
  return args;
}

function _fastIoRead(key) {
  try {
    const result = execSync(
      `openclaw.invoke --tool fast-io --action read --args-json '{"key":"${key}"}'`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }
    );
    return JSON.parse(result);
  } catch (e) { return null; }
}

function _fastIoList(prefix) {
  try {
    const result = execSync(
      `openclaw.invoke --tool fast-io --action list --args-json '{"prefix":"${prefix}"}'`,
      { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }
    );
    return JSON.parse(result);
  } catch (e) { return []; }
}

function _defaultIoAdapter(buildId) {
  return {
    readMatrix: () => _fastIoRead(`atlas-builds/${buildId}/requirement-matrix`),
    listDecisions: () => _fastIoList(`atlas-builds/${buildId}/decisions/`)
  };
}

function _defaultStagingDir(buildId) {
  const agentsRoot = process.env.ISOL8_AGENTS_DIR || path.join(os.homedir(), '.openclaw', 'agents');
  return path.join(agentsRoot, `.atlas-staging-${buildId}`);
}

function audit({ buildId, stagingDir, _ioAdapter }) {
  if (!buildId) {
    return { pass: false, error: 'build_id_missing', failures: [] };
  }
  const io = _ioAdapter || _defaultIoAdapter(buildId);
  const staging = stagingDir || _defaultStagingDir(buildId);
  const workspaceDir = path.join(staging, 'workspace');

  const matrix = io.readMatrix();
  if (!matrix) {
    return { pass: false, error: 'matrix_not_found', build_id: buildId, failures: [] };
  }
  const rows = matrix.user_stories || matrix.classifications || [];
  const decisionList = io.listDecisions() || [];
  const decisionsByReqId = {};
  for (const d of decisionList) {
    const reqId = d.req_id || (d.key || '').split('/').pop();
    decisionsByReqId[reqId] = d;
  }

  const failures = [];

  for (const row of rows) {
    const reqId = row.req_id || row.id;
    const decision = decisionsByReqId[reqId] || {};
    if (decision.decision === 'drop') continue;

    const finalClassification = decision.classification_override || row.classification;
    if (finalClassification !== 'deterministic') continue;

    const triggers = row.triggers || [];
    if (triggers.length === 0) {
      failures.push({
        req_id: reqId,
        reason: 'no_triggers',
        message: `deterministic requirement ${reqId} has no triggers. Re-vet to bind a trigger OR reclassify to llm/hybrid.`
      });
      continue;
    }

    for (const t of triggers) {
      if (t.pipeline === null || t.pipeline === undefined) continue;  // agent-call may have null
      const pipelinePath = path.join(workspaceDir, t.pipeline);
      if (!fs.existsSync(pipelinePath)) {
        failures.push({
          req_id: reqId,
          reason: 'pipeline_missing',
          pipeline: t.pipeline,
          kind: t.kind,
          message: `trigger references pipeline ${t.pipeline} but it is not in the staging workspace.`
        });
      }
    }
  }

  return {
    pass: failures.length === 0,
    build_id: buildId,
    staging_dir: staging,
    audited_count: rows.length,
    failures
  };
}

function main() {
  const args = parseArgs(process.argv);
  const result = audit({ buildId: args.buildId, stagingDir: args.stagingDir });
  process.stdout.write(JSON.stringify(result));
  if (!result.pass) process.exit(1);
}

if (require.main === module) main();

module.exports = { audit };
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node --test agents/atlas/tests/determinism-audit.test.js`
Expected: all 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add agents/atlas/atlas-determinism-audit.js agents/atlas/tests/determinism-audit.test.js
git commit -m "feat(atlas): S9.5 determinism-audit gate (hard block, no override)"
```

---

## Task 8: Wire `register-triggers` + widened rollback into `build.lobster`

**Files:**
- Modify: `agents/atlas/build.lobster`

- [ ] **Step 1: Edit `build.lobster`**

Replace the `# Step 6.10 — Rollback` block (lines 85-89) and the audit-log block (lines 91-109). Final state of those sections:

```yaml
  # Step 6.9.5 — Register triggers (cron rows + hook/agent-call asserts)
  - id: register-triggers
    command: >
      exec --json --shell 'node atlas-trigger-registrar.js --mode=register --build-id="{{build_id}}" --agent-name="{{agent_name}}"'
    condition: $openclaw-agents-add.json.exit_code == 0

  # Step 6.10 — Rollback on agents-add OR register-triggers failure.
  # Deregister-triggers runs FIRST so we clean cron rows we wrote earlier
  # in the same build before deleting the agent dir.
  - id: deregister-triggers
    command: >
      exec --json --shell 'node atlas-trigger-registrar.js --mode=deregister --build-id="{{build_id}}" --agent-name="{{agent_name}}"'
    condition: $openclaw-agents-add.json.exit_code != 0 || $register-triggers.json.success == false

  - id: rollback
    command: >
      exec --json --shell 'openclaw agents delete {{agent_name}} --force'
    condition: $openclaw-agents-add.json.exit_code != 0 || $register-triggers.json.success == false

  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/build/{{build_id}}",
        "data": {
          "action": "build_complete",
          "build_id": "{{build_id}}",
          "agent_name": "{{agent_name}}",
          "clawhavoc_pass": $clawhavoc-scan.json.pass,
          "security_exception_used": $clawhavoc-scan.json.pass == false && $security-exception.json.confirmed,
          "sensitive_warnings_dismissed": $sensitive-sniff.json.warnings,
          "bundle_sha256": $sensitive-sniff.json.bundle_sha256,
          "file_write_confirmed": $file-write-gate.json.confirmed,
          "atomic_mv_succeeded": $verify-write.json.success,
          "openclaw_add_succeeded": $openclaw-agents-add.json.exit_code == 0,
          "register_triggers_success": $register-triggers.json.success,
          "register_triggers_cron_count": $register-triggers.json.cron_registered_count,
          "register_triggers_hook_count": $register-triggers.json.hook_asserted_count,
          "rollback_fired": $openclaw-agents-add.json.exit_code != 0 || $register-triggers.json.success == false,
          "deregister_triggers_cron_removed": $deregister-triggers.json.cron_removed
        }
      }'
```

Concretely, use Edit to replace the block. Old block to replace:

```yaml
  # Step 6.10 — Rollback on add failure (openclaw agents delete recovers to Trash per docs)
  - id: rollback
    command: >
      exec --json --shell 'openclaw agents delete {{agent_name}} --force'
    condition: $openclaw-agents-add.json.exit_code != 0

  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/build/{{build_id}}",
        "data": {
          "action": "build_complete",
          "build_id": "{{build_id}}",
          "agent_name": "{{agent_name}}",
          "clawhavoc_pass": $clawhavoc-scan.json.pass,
          "security_exception_used": $clawhavoc-scan.json.pass == false && $security-exception.json.confirmed,
          "sensitive_warnings_dismissed": $sensitive-sniff.json.warnings,
          "bundle_sha256": $sensitive-sniff.json.bundle_sha256,
          "file_write_confirmed": $file-write-gate.json.confirmed,
          "atomic_mv_succeeded": $verify-write.json.success,
          "openclaw_add_succeeded": $openclaw-agents-add.json.exit_code == 0,
          "rollback_fired": $openclaw-agents-add.json.exit_code != 0
        }
      }'
```

New block (replaces it):

```yaml
  # Step 6.9.5 — Register triggers (cron rows + hook/agent-call asserts)
  - id: register-triggers
    command: >
      exec --json --shell 'node atlas-trigger-registrar.js --mode=register --build-id="{{build_id}}" --agent-name="{{agent_name}}"'
    condition: $openclaw-agents-add.json.exit_code == 0

  # Step 6.10a — Deregister triggers on rollback (cleans cron rows written this build)
  - id: deregister-triggers
    command: >
      exec --json --shell 'node atlas-trigger-registrar.js --mode=deregister --build-id="{{build_id}}" --agent-name="{{agent_name}}"'
    condition: $openclaw-agents-add.json.exit_code != 0 || $register-triggers.json.success == false

  # Step 6.10b — Rollback on agents-add OR register-triggers failure
  - id: rollback
    command: >
      exec --json --shell 'openclaw agents delete {{agent_name}} --force'
    condition: $openclaw-agents-add.json.exit_code != 0 || $register-triggers.json.success == false

  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/build/{{build_id}}",
        "data": {
          "action": "build_complete",
          "build_id": "{{build_id}}",
          "agent_name": "{{agent_name}}",
          "clawhavoc_pass": $clawhavoc-scan.json.pass,
          "security_exception_used": $clawhavoc-scan.json.pass == false && $security-exception.json.confirmed,
          "sensitive_warnings_dismissed": $sensitive-sniff.json.warnings,
          "bundle_sha256": $sensitive-sniff.json.bundle_sha256,
          "file_write_confirmed": $file-write-gate.json.confirmed,
          "atomic_mv_succeeded": $verify-write.json.success,
          "openclaw_add_succeeded": $openclaw-agents-add.json.exit_code == 0,
          "register_triggers_success": $register-triggers.json.success,
          "register_triggers_cron_count": $register-triggers.json.cron_registered_count,
          "register_triggers_hook_count": $register-triggers.json.hook_asserted_count,
          "rollback_fired": $openclaw-agents-add.json.exit_code != 0 || $register-triggers.json.success == false,
          "deregister_triggers_cron_removed": $deregister-triggers.json.cron_removed
        }
      }'
```

- [ ] **Step 2: Verify build.lobster still parses as valid YAML**

Run: `node -e "const yaml=require('child_process').execSync; console.log(require('fs').readFileSync('agents/atlas/build.lobster','utf8').length, 'bytes')"`
Expected: a byte count (just confirms file is readable). Then verify pipeline contains both new steps:

Run: `grep -E "^  - id: (register-triggers|deregister-triggers|rollback)" agents/atlas/build.lobster`
Expected output:
```
  - id: register-triggers
  - id: deregister-triggers
  - id: rollback
```

- [ ] **Step 3: Commit**

```bash
git add agents/atlas/build.lobster
git commit -m "feat(atlas): build.lobster — register-triggers step + widened rollback"
```

---

## Task 9: Insert S9.5 gate into `recap-and-cost.lobster`

**Files:**
- Modify: `agents/atlas/recap-and-cost.lobster`

- [ ] **Step 1: Edit `recap-and-cost.lobster`**

Insert a new step after `architecture-signoff` (line 51-59) and BEFORE the `audit-log` step (line 62). The new step calls `atlas-determinism-audit.js`. If `result.pass` is false, the pipeline halts (no override).

Use Edit to insert the new step. Find the existing block:

```yaml
  # Step 5.7 — S9 architecture sign-off agentgate (sync hard block)
  - id: architecture-signoff
    command: >
      openclaw.invoke --tool agentgate --action confirm --args-json '{
        "gate_id": "S9_architecture_signoff",
        "build_id": "{{build_id}}",
        "summary": "Architecture sign-off — proceed to Step 6 file generation?",
        "block_until_confirmed": true
      }'
    approval: required

  # Audit log
  - id: audit-log
```

Replace with:

```yaml
  # Step 5.7 — S9 architecture sign-off agentgate (sync hard block)
  - id: architecture-signoff
    command: >
      openclaw.invoke --tool agentgate --action confirm --args-json '{
        "gate_id": "S9_architecture_signoff",
        "build_id": "{{build_id}}",
        "summary": "Architecture sign-off — proceed to Step 6 file generation?",
        "block_until_confirmed": true
      }'
    approval: required

  # Step 5.8 — S9.5 determinism-audit gate (HARD; no override per spec §5).
  # Asserts every requirement classified `deterministic` has triggers.length > 0
  # AND every trigger's pipeline exists in the staging dir. On failure, the
  # pipeline halts and the user must re-vet to bind triggers OR reclassify
  # the requirement to llm/hybrid.
  - id: determinism-audit
    command: >
      exec --json --shell 'node atlas-determinism-audit.js --build-id="{{build_id}}"'
    condition: $architecture-signoff.json.confirmed == true

  # Step 5.9 — Surface failures to the user when the gate refuses to advance.
  # The audit script exits non-zero on failure, which stops the pipeline; this
  # step posts the failure list to the vet channel so the user knows what to fix.
  - id: determinism-failure-notice
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "S9.5 determinism-audit HALTED the build. $determinism-audit.json.failures.length requirement(s) need either a trigger binding or a reclassification:\n$determinism-audit.json.failures"
      }'
    condition: $determinism-audit.json.pass == false

  # Audit log
  - id: audit-log
```

Also extend the audit-log block (last block in the file, lines 62-73) to include S9.5 result. Current block:

```yaml
  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/recap-and-cost/{{build_id}}",
        "data": {
          "action": "recap_and_cost_complete",
          "build_id": "{{build_id}}",
          "matrix_hash_verified": $verify-matrix-hash.json.hash_matches,
          "replacement_ratio": $cost-count.json.replacement_ratio,
          "architecture_signoff": "$architecture-signoff.json.confirmed"
        }
      }'
```

Replace with:

```yaml
  # Audit log
  - id: audit-log
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "audit/{{timestamp}}/recap-and-cost/{{build_id}}",
        "data": {
          "action": "recap_and_cost_complete",
          "build_id": "{{build_id}}",
          "matrix_hash_verified": $verify-matrix-hash.json.hash_matches,
          "replacement_ratio": $cost-count.json.replacement_ratio,
          "architecture_signoff": "$architecture-signoff.json.confirmed",
          "determinism_audit_pass": $determinism-audit.json.pass,
          "determinism_audit_failures": $determinism-audit.json.failures
        }
      }'
```

- [ ] **Step 2: Verify the file still has expected step IDs**

Run: `grep -E "^  - id:" agents/atlas/recap-and-cost.lobster`
Expected output (in this order):
```
  - id: verify-matrix-hash
  - id: recap-diff
  - id: persist-recap
  - id: cost-count
  - id: persist-cost
  - id: present-summary
  - id: architecture-signoff
  - id: determinism-audit
  - id: determinism-failure-notice
  - id: audit-log
```

- [ ] **Step 3: Commit**

```bash
git add agents/atlas/recap-and-cost.lobster
git commit -m "feat(atlas): S9.5 determinism-audit gate after S9 architecture sign-off"
```

---

## Task 10: Thread `triggers[]` schema into `architect-proposal.lobster`

**Files:**
- Modify: `agents/atlas/architect-proposal.lobster`

The `archetype-synthesis` LLM call (Step 3.6) already produces `user_stories` + `voice_tone`. Add `requirements_with_triggers` to the same response so we get triggers without a new LLM call. Persist it into the matrix snapshot (Step 3.7).

- [ ] **Step 1: Edit `architect-proposal.lobster`**

Replace the existing `archetype-synthesis` block (lines 42-51):

```yaml
  # Step 3.6 — Archetype synthesis (1 LLM call; produces user-stories AND SOUL.md voice/tone in shared schema)
  - id: archetype-synthesis
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "Phrase each requirement as a user-story (As a [agent], I should [action] so that [outcome]) and produce SOUL.md voice/tone guidance for the agent archetype.",
        "input": {"skeleton": $skeleton-generate.json, "intake": $load-intake.json, "brief": $load-brief.json},
        "schema": {"type":"object","properties":{"schema_version":{"type":"string"},"user_stories":{"type":"array"},"voice_tone":{"type":"object"}},"required":["schema_version","user_stories","voice_tone"]},
        "thinking": "low",
        "maxTokens": 2500
      }'
```

With this expanded block:

```yaml
  # Step 3.6 — Archetype synthesis (1 LLM call; produces user-stories,
  # voice/tone, AND per-requirement triggers[] binding in the same response.
  # No new LLM call — triggers ride on the existing multi-field-output schema.
  #
  # Schema notes for the LLM:
  #   requirements_with_triggers[i].req_id           — matches user_stories[].req_id
  #   requirements_with_triggers[i].classification   — "deterministic" | "llm" | "hybrid"
  #                                                    (echo the classifier'\''s assignment)
  #   requirements_with_triggers[i].triggers[]       — array of triggers; for `deterministic`
  #                                                    classification MUST be non-empty
  #   trigger kinds:
  #     cron       — {kind, schedule, tz?, pipeline}   — pipeline ends in .lobster
  #     hook       — {kind, route, pipeline}            — route starts with /hooks/
  #     agent-call — {kind, callable_as, pipeline|null}
  #   `channel` kind is NOT supported in v1.
  - id: archetype-synthesis
    command: >
      openclaw.invoke --tool llm-task --action json --args-json '{
        "prompt": "For each requirement: (1) phrase as user-story (As a [agent], I should [action] so that [outcome]); (2) propose triggers[] binding the requirement to runtime entry points. Triggers MUST be non-empty when classification is `deterministic`. Trigger kinds: cron (schedule + .lobster pipeline), hook (/hooks/ route + .lobster pipeline), agent-call (callable_as + optional .lobster pipeline). Also produce SOUL.md voice/tone for the archetype.",
        "input": {"skeleton": $skeleton-generate.json, "classifications": $det-llm-classify.json, "intake": $load-intake.json, "brief": $load-brief.json},
        "schema": {"type":"object","properties":{"schema_version":{"type":"string"},"user_stories":{"type":"array"},"voice_tone":{"type":"object"},"requirements_with_triggers":{"type":"array","items":{"type":"object","properties":{"req_id":{"type":"string"},"classification":{"type":"string","enum":["deterministic","llm","hybrid"]},"triggers":{"type":"array","items":{"type":"object","properties":{"kind":{"type":"string","enum":["cron","hook","agent-call"]},"schedule":{"type":"string"},"tz":{"type":"string"},"route":{"type":"string"},"callable_as":{"type":"string"},"pipeline":{"type":["string","null"]}},"required":["kind"]}}},"required":["req_id","classification","triggers"]}}},"required":["schema_version","user_stories","voice_tone","requirements_with_triggers"]},
        "thinking": "low",
        "maxTokens": 3500
      }'
```

Also replace the snapshot-matrix block (lines 53-66) to persist the new field:

```yaml
  # Step 3.7 — Snapshot original requirement matrix (W5 hash-verification pattern)
  - id: snapshot-matrix
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-builds/{{build_id}}/requirement-matrix",
        "data": {
          "skeleton": $skeleton-generate.json,
          "classifications": $det-llm-classify.json,
          "skill_recommendations": $skill-recommend.json,
          "user_stories": $archetype-synthesis.json.user_stories,
          "voice_tone": $archetype-synthesis.json.voice_tone,
          "requirements_with_triggers": $archetype-synthesis.json.requirements_with_triggers,
          "snapshot_at": "{{timestamp}}"
        }
      }'
```

- [ ] **Step 2: Verify the field is referenced everywhere it needs to be**

Run: `grep -n "requirements_with_triggers" agents/atlas/architect-proposal.lobster`
Expected: 3 matches (the LLM schema, the snapshot-matrix data block, and the prompt hint comment in the LLM block).

- [ ] **Step 3: Commit**

```bash
git add agents/atlas/architect-proposal.lobster
git commit -m "feat(atlas): architect-proposal — emit per-requirement triggers[] via archetype-synthesis"
```

---

## Task 11: Surface `triggers[]` in vet-walk + accept user edits

**Files:**
- Modify: `agents/atlas/atlas-requirement-walker.js`
- Modify: `agents/atlas/vet-walk.lobster`

The walker's `presentNext` already returns the row's data. Add `triggers` to the returned object and add an `--action=apply-trigger-edits` action that accepts the user's edits and merges them into the per-requirement decision record.

- [ ] **Step 1: Modify `atlas-requirement-walker.js`**

Edit `agents/atlas/atlas-requirement-walker.js`. Inside `presentNext`, replace the `return { more_outstanding: true, ... }` block (lines 82-88) with:

```javascript
      // Find triggers for this req in the matrix's requirements_with_triggers
      // field (added by architect-proposal Step 3.6). Falls back to [] for
      // legacy matrices that pre-date the field.
      const triggersByReq = {};
      for (const rt of (fullMatrix.requirements_with_triggers || [])) {
        triggersByReq[rt.req_id] = rt;
      }
      const triggerRow = triggersByReq[reqId] || { triggers: [], classification: req.classification || null };

      return {
        more_outstanding: true,
        req_id: reqId,
        requirement_text: req.action ? `${req.action}` : (req.text || JSON.stringify(req)),
        rationale: req.rationale || '(rationale generated by skeleton)',
        classification: triggerRow.classification,
        triggers: triggerRow.triggers,
        presented_at: new Date().toISOString()
      };
```

Add a new action handler. Add this function after `checkLoop` (around line 103):

```javascript
function applyTriggerEdits(buildId, reqId, edits) {
  // Edits shape: { decision: 'approve'|'change'|'drop', user_note: string,
  //                triggers?: [...], classification_override?: 'llm'|'hybrid'|'deterministic' }
  // The walker writes the decision row; this helper just normalizes the edit
  // payload so vet-walk.lobster can use a single command.
  return {
    req_id: reqId,
    build_id: buildId,
    decision: edits.decision,
    user_note: edits.user_note || null,
    triggers: edits.triggers || null,
    classification_override: edits.classification_override || null,
    decided_at: new Date().toISOString()
  };
}
```

In `main()` (around line 105), add a new case before the `else` clause:

```javascript
  } else if (args.action === 'apply-trigger-edits') {
    const edits = readStdinJson();
    if (!args.reqId) { console.error('--req-id required'); process.exit(2); }
    result = applyTriggerEdits(args.buildId, args.reqId, edits);
```

And add `--req-id=` parsing in `parseArgs` (line 17-23):

```javascript
function parseArgs(argv) {
  const args = { action: 'load', buildId: null, reqId: null };
  for (const a of argv.slice(2)) {
    if (a.startsWith('--action=')) args.action = a.split('=')[1];
    else if (a.startsWith('--build-id=')) args.buildId = a.split('=')[1];
    else if (a.startsWith('--req-id=')) args.reqId = a.split('=')[1];
  }
  return args;
}
```

Add `readStdinJson` helper (just below `parseArgs`):

```javascript
function readStdinJson() {
  try { return JSON.parse(require('fs').readFileSync(0, 'utf8')); } catch (e) { return {}; }
}
```

Update `module.exports`:

```javascript
module.exports = { STRONG_SIGNAL_TOKENS, detectAntiOvercomplicationSignal, loadMatrix, presentNext, applyTriggerEdits };
```

- [ ] **Step 2: Modify `vet-walk.lobster` to render triggers in the prompt**

Edit `agents/atlas/vet-walk.lobster`. Replace the `present-row` block (lines 35-41):

```yaml
  # Step 4.4 — Present row to user with rationale, gate on response
  - id: present-row
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "$walker-iteration.json.req_id: $walker-iteration.json.requirement_text\n\nRationale: $walker-iteration.json.rationale\n\nApprove / change (with what) / drop (with why)?"
      }'
    approval: required
```

With this expanded block that surfaces triggers and asks specifically about them when `deterministic`:

```yaml
  # Step 4.4 — Present row to user with rationale + triggers, gate on response.
  # When classification == "deterministic" the user MUST either approve the
  # triggers as drafted, edit them (change kind, schedule, route, pipeline),
  # or reclassify the requirement to llm/hybrid (which removes the trigger
  # obligation). S9.5 will refuse to advance the build otherwise.
  - id: present-row
    command: >
      openclaw.invoke --tool slack --action post --args-json '{
        "channel": "{{vet_channel}}",
        "text": "$walker-iteration.json.req_id ($walker-iteration.json.classification): $walker-iteration.json.requirement_text\n\nRationale: $walker-iteration.json.rationale\n\nTriggers (deterministic-binding):\n$walker-iteration.json.triggers\n\nApprove / change (triggers and/or classification) / drop (with why)?"
      }'
    approval: required
```

Then replace the `record-decision` block (lines 43-55) to persist the trigger/classification edits via the new walker action:

```yaml
  # Step 4.5 — Record decision per-requirement (now also accepts trigger + classification edits)
  - id: record-decision
    command: >
      exec --json --shell 'node atlas-requirement-walker.js --action=apply-trigger-edits --build-id="{{build_id}}" --req-id="$walker-iteration.json.req_id"'
    stdin: { decision: $present-row.json.decision, user_note: $present-row.json.user_note, triggers: $present-row.json.triggers, classification_override: $present-row.json.classification_override }

  # Step 4.5b — Persist the normalized decision row.
  - id: persist-decision
    command: >
      openclaw.invoke --tool fast-io --action write --args-json '{
        "key": "atlas-builds/{{build_id}}/decisions/$walker-iteration.json.req_id",
        "data": $record-decision.json
      }'
```

- [ ] **Step 3: Verify walker tests still pass (no regression)**

Run: `node --test agents/atlas/tests/`
Expected: all existing tests still pass (we did not break `loadMatrix`, `checkLoop`, or `detectAntiOvercomplicationSignal`).

Then quick smoke test of `applyTriggerEdits`:

Run: `node -e "const w=require('./agents/atlas/atlas-requirement-walker.js'); console.log(JSON.stringify(w.applyTriggerEdits('B1','R1',{decision:'approve',user_note:'ok',triggers:[{kind:'cron'}]})))"`
Expected: JSON with `req_id: 'R1'`, `decision: 'approve'`, `triggers: [{kind: 'cron'}]`.

- [ ] **Step 4: Commit**

```bash
git add agents/atlas/atlas-requirement-walker.js agents/atlas/vet-walk.lobster
git commit -m "feat(atlas): vet-walk surfaces triggers[] per requirement + accepts edits"
```

---

## Task 12: Append "Determinism Contract" section to `AGENTS.md`

**Files:**
- Modify: `agents/atlas/AGENTS.md`

- [ ] **Step 1: Find the insertion point**

The "Build Session Learnings" appendix (line 13 onward) must NOT be touched. We insert the new section just BEFORE the existing "Audit Trail" section.

Run: `grep -n "^## " agents/atlas/AGENTS.md` to confirm the section order.
Expected output ends with a line like `XXX:## Audit Trail`. We append immediately before that section.

- [ ] **Step 2: Insert the new section**

Use Edit to insert a new section. Find the existing line `## Audit Trail` and prepend the new section above it. The Edit's `old_string` should match the exact `## Audit Trail` line plus the blank line above it; `new_string` should be the new section followed by the same `## Audit Trail` line.

Insert:

```markdown
## Determinism Contract

Atlas refuses to ship an agent that claims a deterministic requirement without binding it to a runtime trigger. The contract is enforced as code at two gates:

- **Step 3.6 architect-proposal** emits a per-requirement `triggers[]` field via the existing `archetype-synthesis` LLM call (no new LLM call — triggers ride on the same multi-field-output schema).
- **Step 4 vet-walk** surfaces those triggers per requirement; the user can approve, edit the schedule/route/pipeline, or reclassify the requirement (which removes the obligation).
- **Step 5.8 S9.5 determinism-audit** asserts every requirement whose final classification is `deterministic` has `triggers.length > 0` AND every trigger with a non-null `pipeline` references a `.lobster` file present in the staging dir. **Hard block. No agentgate override.** Matches the same risk posture as ClawHavoc and the file-write-gate.
- **Step 6.9.5 register-triggers** writes cron rows into `~/.openclaw/cron/jobs.json` (direct JSON write to the persisted shape — no `openclaw cron add` CLI). Hook triggers are asserted no-op because hooks are framework-routed via `gateway.hooks.allowedSessionKeyPrefixes`, not per-route CLI-registered. Channel triggers are NOT supported in v1; the shipped OpenClaw agents declare only global `channels.{provider}.enabled` with no agent-scoped match/route.
- **Step 6.10 widened rollback** fires if EITHER `openclaw agents add` OR `register-triggers` fails. Rollback calls `deregister-triggers` first (which cleans cron rows by `name` prefix `{build_id}-`) and then `openclaw agents delete --force`.

The built agent's runtime workspace contains a `triggers.json` file recording the final wiring. Atlas writes it during Step 6.7 atomic file generation under `~/.openclaw/agents/{name}/workspace/triggers.json`. After register-triggers succeeds, each entry's `registered_as` field carries the registrar's job-name (e.g. `build_2026-05-25_abc123-R7-1`) so a future reconciler can confirm runtime state matches build-time state.

**Cron-job naming.** Each registered cron row has `name = "{build_id}-{req_id}-{n}"` (n is 1-indexed across the build's triggers for that req_id) and `id = <fresh UUID v4>`. The build_id-prefixed name makes rollback's per-build cleanup cheap (filter by prefix) without colliding across rebuilds.

**Failure UX.** When S9.5 refuses to advance, the user sees the list of offending `req_id`s and their missing bindings on the vet channel. The user resolves by either (a) re-vetting to bind triggers or (b) reclassifying the requirement to llm/hybrid via a walker edit. Both paths are audit-logged.

```

- [ ] **Step 3: Verify the BSL appendix and existing sections are intact**

Run: `grep -n "^## " agents/atlas/AGENTS.md`
Expected: includes BOTH `## Build Session Learnings — Methodology Working As Designed` AND `## Determinism Contract` AND `## Audit Trail` (in that order), plus all the existing other sections.

Run: `grep -c "^## " agents/atlas/AGENTS.md`
Expected: count is original count + 1.

- [ ] **Step 4: Commit**

```bash
git add agents/atlas/AGENTS.md
git commit -m "docs(atlas): AGENTS.md — Determinism Contract section explaining S9.5 + register-triggers"
```

---

## Task 13: Integration smoke test — end-to-end synthetic build

**Files:**
- Create: `agents/atlas/tests/integration-smoke.test.js`

A scripted end-to-end run against a synthetic 3-requirement spec. Asserts the staging tree lands under `workspace/`, the cron store gets one row, the rollback path fully unwinds cron + agent dir.

- [ ] **Step 1: Write the test**

Create `agents/atlas/tests/integration-smoke.test.js`:

```javascript
// End-to-end synthetic build smoke test. Exercises:
//   draftToFastIo → stages workspace/ tree + manifest
//   writeDisk      → atomic mv into agentsRoot/{name}/
//   register       → writes cron row into ATLAS_CRON_STORE
//   deregister     → removes only this build's rows

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

function makeRoots() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'atlas-e2e-'));
  return {
    agentsRoot: path.join(root, 'agents'),
    cronStore: path.join(root, 'cron', 'jobs.json')
  };
}

function specFor(buildId) {
  return {
    build_id: buildId,
    intake: { name: 'e2ebot', archetype: 'general', build_id: buildId },
    user_stories: [
      { req_id: 'R1', agent_name: 'e2ebot', action: 'send weekly digest', outcome: 'team sees status weekly' },
      { req_id: 'R2', agent_name: 'e2ebot', action: 'respond to transcript', outcome: 'meeting notes captured' },
      { req_id: 'R3', agent_name: 'e2ebot', action: 'answer questions', outcome: 'user gets answers' }
    ],
    never_does: ['never spam channels'],
    pipelines: [
      { name: 'weekly-digest', purpose: 'cron-driven weekly summary' },
      { name: 'meeting-process', purpose: 'hook-driven transcript handler' }
    ],
    requirements_with_triggers: [
      { req_id: 'R1', classification: 'deterministic',
        triggers: [{ kind: 'cron', schedule: '0 8 * * 1', tz: 'America/New_York', pipeline: 'weekly-digest.lobster' }] },
      { req_id: 'R2', classification: 'deterministic',
        triggers: [{ kind: 'hook', route: '/hooks/transcript-ready', pipeline: 'meeting-process.lobster' }] },
      { req_id: 'R3', classification: 'llm', triggers: [] }
    ]
  };
}

function stageThenWrite(spec, agentsRoot) {
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  fs.mkdirSync(agentsRoot, { recursive: true });
  const gen = require('../atlas-skeleton-generator.js');
  const drafted = gen.draftToFastIo(spec);
  assert.ok(drafted.pass, JSON.stringify(drafted.errors));

  // Materialize to the staging dir as build.lobster step 6.7 would.
  const stagingDir = path.join(agentsRoot, `.atlas-staging-${spec.build_id}`);
  fs.mkdirSync(stagingDir, { recursive: true });
  for (const [rel, content] of Object.entries(drafted.artifacts_to_stage)) {
    const full = path.join(stagingDir, rel);
    fs.mkdirSync(path.dirname(full), { recursive: true });
    fs.writeFileSync(full, content);
  }
  return gen.writeDisk(spec.build_id, spec.intake.name);
}

test('end-to-end: stage → writeDisk → register places workspace tree + cron row', () => {
  const { agentsRoot, cronStore } = makeRoots();
  process.env.ATLAS_CRON_STORE = cronStore;

  const spec = specFor('build_e2e_happy');
  const written = stageThenWrite(spec, agentsRoot);
  assert.ok(written.success, JSON.stringify(written));

  // Workspace tree is on disk.
  const dest = path.join(agentsRoot, 'e2ebot');
  for (const f of ['AGENTS.md', 'SOUL.md', 'openclaw.json', 'triggers.json',
                   'weekly-digest.lobster', 'meeting-process.lobster']) {
    assert.ok(fs.existsSync(path.join(dest, 'workspace', f)), `missing ${f}`);
  }
  assert.ok(fs.existsSync(path.join(dest, 'build-manifest.json')));

  // Register triggers.
  const reg = require('../atlas-trigger-registrar.js');
  const regResult = reg.register({ buildId: spec.build_id, agentName: spec.intake.name });
  assert.ok(regResult.success, JSON.stringify(regResult));
  assert.equal(regResult.cron_registered_count, 1);
  assert.equal(regResult.hook_asserted_count, 1);

  // Cron store has the row.
  const store = JSON.parse(fs.readFileSync(cronStore, 'utf8'));
  assert.equal(store.jobs.length, 1);
  assert.equal(store.jobs[0].name, 'build_e2e_happy-R1-1');
  assert.equal(store.jobs[0].agentId, 'e2ebot');

  // triggers.json was updated with registered_as.
  const tj = JSON.parse(fs.readFileSync(path.join(dest, 'workspace', 'triggers.json'), 'utf8'));
  const cronEntry = tj.triggers.find(t => t.kind === 'cron');
  assert.equal(cronEntry.registered_as, 'build_e2e_happy-R1-1');
  const hookEntry = tj.triggers.find(t => t.kind === 'hook');
  assert.equal(hookEntry.registered_as, 'framework-routed:/hooks/transcript-ready');
});

test('end-to-end: rollback cleans cron rows for the failing build', () => {
  const { agentsRoot, cronStore } = makeRoots();
  process.env.ATLAS_CRON_STORE = cronStore;

  const spec = specFor('build_e2e_rollback');
  const written = stageThenWrite(spec, agentsRoot);
  assert.ok(written.success);

  // Register half the triggers, then deregister to simulate rollback.
  const reg = require('../atlas-trigger-registrar.js');
  reg.register({ buildId: spec.build_id, agentName: spec.intake.name });

  let store = JSON.parse(fs.readFileSync(cronStore, 'utf8'));
  assert.equal(store.jobs.length, 1);

  const dereg = reg.deregister({ buildId: spec.build_id, agentName: spec.intake.name });
  assert.ok(dereg.success);
  assert.equal(dereg.cron_removed, 1);

  store = JSON.parse(fs.readFileSync(cronStore, 'utf8'));
  assert.equal(store.jobs.length, 0);
});

test('end-to-end: determinism-audit refuses build when deterministic req has no triggers', () => {
  const { agentsRoot } = makeRoots();
  process.env.ISOL8_AGENTS_DIR = agentsRoot;
  const spec = specFor('build_e2e_audit_fail');
  // Force R1 to deterministic with NO triggers.
  spec.requirements_with_triggers[0].triggers = [];
  stageThenWrite(spec, agentsRoot);

  const { audit } = require('../atlas-determinism-audit.js');
  const result = audit({
    buildId: spec.build_id,
    stagingDir: path.join(agentsRoot, `.atlas-staging-${spec.build_id}`),
    _ioAdapter: {
      readMatrix: () => ({
        user_stories: spec.requirements_with_triggers.map(r => ({ ...r }))
      }),
      listDecisions: () => spec.requirements_with_triggers.map(r => ({ req_id: r.req_id, decision: 'approve' }))
    }
  });
  // Wait — the staging dir for build_e2e_audit_fail has been mv'd to agentsRoot/e2ebot/.
  // Re-stage so audit can find pipeline files in the staging path.
  const stagingDir = path.join(agentsRoot, `.atlas-staging-${spec.build_id}-audit`);
  fs.mkdirSync(path.join(stagingDir, 'workspace'), { recursive: true });
  fs.writeFileSync(path.join(stagingDir, 'workspace', 'weekly-digest.lobster'), 'name: x\n');
  const result2 = audit({
    buildId: spec.build_id,
    stagingDir,
    _ioAdapter: {
      readMatrix: () => ({ user_stories: spec.requirements_with_triggers.map(r => ({ ...r })) }),
      listDecisions: () => spec.requirements_with_triggers.map(r => ({ req_id: r.req_id, decision: 'approve' }))
    }
  });
  assert.ok(!result2.pass);
  assert.equal(result2.failures.length, 1);
  assert.equal(result2.failures[0].req_id, 'R1');
  assert.match(result2.failures[0].reason, /no_triggers/);
});
```

- [ ] **Step 2: Run the integration test**

Run: `node --test agents/atlas/tests/integration-smoke.test.js`
Expected: all 3 tests pass.

- [ ] **Step 3: Run the full Atlas test suite**

Run: `node --test agents/atlas/tests/`
Expected: every test from tasks 1-13 passes (counts roughly: triggers-spec 9 + recursive-write 4 + trigger-registrar 7 + determinism-audit 6 + integration-smoke 3 = 29).

- [ ] **Step 4: Commit**

```bash
git add agents/atlas/tests/integration-smoke.test.js
git commit -m "test(atlas): integration smoke — staging → write → register → rollback"
```

---

## Self-Review Checklist

Run these checks against the saved plan before invoking executor:

**1. Spec coverage:**
- [x] Change 1 — `triggers[]` field on every requirement-matrix row → Tasks 1 (schema) + 10 (architect-proposal) + 11 (vet-walk).
- [x] Change 2 — Skeleton generator writes a tree (workspace/ root) → Tasks 2 + 3.
- [x] Change 3 — `register-triggers` build step → Tasks 5 + 6 + 8.
- [x] Change 4 — Widened rollback (deregister-triggers + agent delete) → Task 8.
- [x] Change 5 — S9.5 determinism-audit (hard block) → Tasks 7 + 9.
- [x] `triggers.json` emitted to built workspace → Task 2.
- [x] `build-manifest.json` in staging → Task 2.
- [x] Cron-job-id naming (Q3) → resolved in conventions; Task 5 implements.
- [x] Hook-CLI question (Q1) → resolved (no CLI); Task 5 hook branch is no-op assert.
- [x] Channel-binding question (Q2) → resolved (drop in v1); Task 5 rejects `channel` kind.
- [x] Cron-mechanism question (Q4) → resolved (direct JSON write); Task 5 implements.
- [x] AGENTS.md determinism-contract section (do NOT touch BSL) → Task 12.

**2. Placeholder scan:** No `TBD`, no "similar to Task N", no "add appropriate error handling", no bare "implement later". Every step shows complete code or exact text changes.

**3. Type consistency:**
- `register({buildId, agentName})` signature is consistent across Tasks 5, 6, 13.
- `deregister({buildId, agentName})` signature is consistent across Tasks 5, 6, 8, 13.
- `audit({buildId, stagingDir, _ioAdapter})` signature is consistent across Tasks 7, 13.
- `loadManifest(agentName)` defined Task 4, used implicitly by registrar's `_loadManifest` (private; intentionally not the export — registrar reads manifest directly to avoid coupling to the generator's load semantics).
- Cron-job `name` format `{build_id}-{req_id}-{n}` is consistent across Tasks 5 (build), 6 (deregister filter), 13 (e2e assert).
- `--mode=register`/`--mode=deregister` CLI flag consistent between Task 5 (script) and Task 8 (build.lobster wiring).

---

## Execution Notes

- All tests use Node 22's built-in `node:test` runner. No `npm install` required.
- Run a single test file: `node --test agents/atlas/tests/<file>`
- Run all Atlas tests: `node --test agents/atlas/tests/`
- The cron store path defaults to `~/.openclaw/cron/jobs.json`; tests override via `ATLAS_CRON_STORE` env var.
- The agents root defaults to `~/.openclaw/agents/`; tests override via `ISOL8_AGENTS_DIR` env var (existing convention, see `atlas-skeleton-generator.js:446`).
- This plan does NOT touch any backend code, the Isol8 runtime, or `apps/`. Plan B (aligning the runtime `workspaces/{paperclip-uuid}/` layout with what Atlas writes) is a separate plan.
