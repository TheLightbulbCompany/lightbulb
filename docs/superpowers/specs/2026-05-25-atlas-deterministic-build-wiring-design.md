# Atlas Deterministic Build Wiring — Design

**Date:** 2026-05-25
**Status:** Spec, awaiting plan
**Owner area:** `agents/atlas/`

## Problem

Atlas (the meta-agent / agent-builder) produces agents that look like agents
on disk — `openclaw.json`, `AGENTS.md`, `SOUL.md`, `.lobster` pipelines, JS
scripts — but those built agents are not actually *deterministic at
runtime*. Three failures compound:

1. **Lobster pipelines and JSON land in the wrong place on disk.** The
   skeleton-generator's `writeDisk` reads only files at the staging-dir
   root and writes them flat into `~/.openclaw/agents/{name}/`. Any
   subdirectories Atlas drafts in staging (`skills/`, nested workspace
   structure) are dropped. The built agent is a flat pile of files
   alongside the runtime workspace, not inside it. See
   [atlas-skeleton-generator.js:484](../../../agents/atlas/atlas-skeleton-generator.js#L484).

2. **Files land one directory above the configured workspace path.** Built
   `openclaw.json` declares
   `agents.defaults.workspace = "~/.openclaw/agents/{name}/workspace"`
   (the same expression Atlas's own openclaw.json declares at
   [openclaw.json:4](../../../agents/atlas/openclaw.json#L4)), but
   `writeDisk` writes to `~/.openclaw/agents/{name}/` with no
   `workspace/` suffix. The repo-shipped seed agents (Atlas, Echo, Vera,
   Pulse, Ora, etc.) are intentionally flat at `agents/{name}/` for
   source-control simplicity, but the *runtime* layout the seed-deploy
   creates on EFS, and the layout `openclaw.json#agents.defaults.workspace`
   points at, splits config-at-root from workspace-at-`{name}/workspace/`.
   Atlas needs to produce the runtime layout — not the repo-flat layout —
   when it builds.

3. **No triggers are wired after the agent is registered.**
   [build.lobster:80](../../../agents/atlas/build.lobster#L80) runs
   `openclaw agents add` and stops. It never runs `openclaw cron add` for
   declared cron jobs (the skeleton generator emits those as markdown
   docs only at [atlas-skeleton-generator.js:261](../../../agents/atlas/atlas-skeleton-generator.js#L261),
   never executes the commands), and never registers hook routes
   referenced inside the agent's `.lobster` files. The `cron` block in
   `openclaw.json` is enabled but no jobs are ever loaded into
   `~/.openclaw/cron/jobs.json`.

The deeper missing piece: Atlas's architect-proposal step classifies
requirements as `deterministic / llm / hybrid`, but there is no
requirement-matrix field capturing **how** a deterministic capability is
triggered (cron schedule? hook route? channel match? agent call?). The
classifier asserts a claim that the build step has no contract to
enforce.

## Design Goals

- **Claiming deterministic creates an obligation to wire.** Atlas should
  refuse to ship an agent that claims a deterministic requirement without
  binding it to a runtime trigger.
- **Files land where the runtime reads.** The on-disk path Atlas writes
  must match `openclaw.json#agents.defaults.workspace`.
- **Triggers are first-class structured data**, not free-text markdown,
  threaded from architect-proposal through vet through build and
  preserved in the built agent for introspection.
- **The methodology is the implementation.** The new gate and the new
  build step live in `.lobster` pipelines, not in prose. Drift requires
  a code change.
- **No new LLM calls.** Trigger structuring rides on the existing
  archetype-synthesis multi-field-output schema.

## Non-Goals

- Re-wiring Atlas itself. Atlas's own `openclaw.json`, cron block, and
  hook path are correct as-is; the changes here only affect agents Atlas
  *builds*.
- Changing the 6-step methodology, hard rules, ClawHavoc/sensitive-sniff
  scans, citation discipline, identity lock, S10/S11 agentgates, or
  marketplace publish (Step 7).
- Changing the marketplace consumed-side deploy
  (`catalog_service.deploy_from_artifact`). `triggers.json` travels in
  the bundle; consumed-side wiring is the buyer's deploy logic, out of
  scope here.
- Replacing the local-host fallback. `process.env.ISOL8_AGENTS_DIR`
  stays for dev. The Isol8-container path is the same expression
  (`~/.openclaw/agents/{name}/workspace/`) — that already resolves to
  the right EFS mount inside the OpenClaw container.

## The Five Changes

### 1. `triggers[]` field on every requirement-matrix row

Captured during Step 2 architect-proposal, threaded through Step 4 vet,
persisted into the build manifest.

```jsonc
{
  "req_id": "R7",
  "classification": "deterministic",
  "user_story": "As echo, I should summarize meetings so that ...",
  "triggers": [
    { "kind": "cron",    "schedule": "0 8 * * 1",
      "pipeline": "weekly-digest.lobster" },
    { "kind": "hook",    "route": "/hooks/transcript-ready",
      "pipeline": "meeting-process.lobster" },
    { "kind": "channel", "channel": "slack", "match": "@echo",
      "pipeline": "meeting-process.lobster" },
    { "kind": "agent-call", "callable_as": "agent.invoke('echo', ...)",
      "pipeline": null }
  ]
}
```

Field rules:

- `kind` ∈ `{cron, hook, channel, agent-call}`.
- For `cron` / `hook` / `channel`, `pipeline` MUST be a non-null
  `.lobster` filename that Atlas is also generating in the staging dir.
  Verified at write time.
- For `agent-call`, `pipeline` MAY be null (callable agents do not need a
  bound pipeline; the agent's own AGENTS.md instructs it on what to do
  on invocation).
- A requirement classified `deterministic` MUST have
  `triggers.length > 0` (enforced by S9.5; see below).
- A requirement classified `llm` or `hybrid` MAY have
  `triggers.length >= 0` — triggers are still informative but not
  required.

The field is produced by the existing `archetype-synthesis` LLM call
during architect-proposal as an added field on the same multi-field
output schema — no new LLM call. The skeleton-generator's template walker
consumes it.

### 2. Skeleton generator writes a tree, not a flat dir

Replace the file-only walk at
[atlas-skeleton-generator.js:484](../../../agents/atlas/atlas-skeleton-generator.js#L484):

```js
// today
const stagedFiles = fs.readdirSync(stagingDir, { withFileTypes: true })
  .filter(d => d.isFile())
  .map(d => d.name);
```

with a recursive walk that preserves staging-dir subdirectories.
SHA256s are computed per-file across the recursive set; verify-write
walks the same tree.

The generator's template grows a `workspace/` root: all generated files
(`AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `TOOLS.md`,
`BOOTSTRAP.md`, `openclaw.json`, every `.lobster`, every `.js`,
`triggers.json`, and any `skills/` subdir) land under
`agents/{name}/workspace/`. The container's runtime workspace path
already resolves to that location.

Destination: `path.join(agentsRoot, agentName)` — unchanged — but the
staging tree the generator emits now has `workspace/` as its top-level
folder, so the post-rename layout is
`~/.openclaw/agents/{name}/workspace/...` matching
`openclaw.json#agents.defaults.workspace`.

`openclaw.json` itself stays at `~/.openclaw/agents/{name}/openclaw.json`
(the level *above* the workspace) — that's the OpenClaw runtime
convention `agents.defaults.workspace` already encodes. The repo-flat
seed-agent layout is a source-control convenience; the runtime EFS
layout, which is what Atlas's build targets, is config-at-root +
workspace-at-`{name}/workspace/`.

### 3. New build step 6.9.5 — `register-triggers`

Added to `build.lobster` after step 6.9 (`openclaw-agents-add`) and
before step audit-log. Reads the build manifest's flattened `triggers[]`
(union across all requirements) and registers each:

```yaml
- id: register-triggers
  command: >
    exec --json --shell 'node atlas-trigger-registrar.js
      --build-id="{{build_id}}"
      --agent-name="{{agent_name}}"'
  condition: $openclaw-agents-add.json.exit_code == 0
```

The new `atlas-trigger-registrar.js` script:

- `cron` → shells `openclaw cron add {req_id}-{n} "{schedule}" "lobster run {pipeline}" --session isolated --light-context`
- `hook` → shells `openclaw hooks register {route} --pipeline {pipeline}`
  (CLI shape TBD — see Open Question)
- `channel` → writes the binding into the agent's `channels.{provider}`
  config (via in-place merge into the already-written `openclaw.json`),
  not a CLI call
- `agent-call` → no registration; asserts the named pipeline exists in
  the workspace (or that `pipeline` is null, which is legal here)

Each registration emits a structured JSON line for the audit log. On
any registration error, the script aborts further registration and
exits non-zero, surfacing which trigger failed.

### 4. Rollback step widens

[build.lobster:86](../../../agents/atlas/build.lobster#L86) currently
runs `openclaw agents delete {name} --force` on agents-add failure. The
widened rollback also fires if `register-triggers` exits non-zero, and
calls `openclaw cron remove` / `openclaw hooks deregister` for any
trigger the registrar successfully registered earlier in the same build
(tracked in the registrar's per-trigger audit output) before deleting
the agent.

### 5. New gate S9.5 — determinism audit (hard block)

Inserted into `recap-and-cost.lobster` after the existing S9
architecture sign-off agentgate and before the build pipeline's S10
file-write-gate.

Implementation: a new `atlas-determinism-audit.js` script. Walks the
requirement matrix; for every row where `classification ==
"deterministic"`:

- Assert `triggers.length > 0`.
- For each trigger with a non-null `pipeline`, assert that pipeline
  exists in the staging dir.
- Collect failures into a structured response.

On failure: the gate refuses to advance, returning the offending
`req_id`s and their missing bindings to the user. The user must either:

- (a) re-vet those rows to bind triggers, OR
- (b) reclassify the requirement as `llm` or `hybrid` — which is itself
  an audit-logged decision through the existing requirement-walker.

**No agentgate override at S9.5.** This matches Atlas's existing
hard-rule posture (ClawHavoc, file-write-gate). The asymmetric override
semantics at build-time vs publish-time are intentional; S9.5 sits at
the determinism-claim boundary and is the same risk class as a
ClawHavoc denylist hit.

## `triggers.json` in the built workspace

Same data as the manifest's flattened `triggers[]`, scoped to that
agent only, emitted at `agents/{name}/workspace/triggers.json` during
file generation. Two consumers:

- The built agent can introspect its own bindings at runtime.
- Any post-deploy verifier (today: ad-hoc; eventually: a periodic
  `openclaw cron list` / `openclaw hooks list` reconciler) can confirm
  runtime state matches build-time state.

Schema mirrors the per-requirement format:

```jsonc
{
  "agent_name": "echo",
  "build_id": "build_2026-05-25_abc123",
  "triggers": [
    { "req_id": "R7", "kind": "cron", "schedule": "0 8 * * 1",
      "pipeline": "weekly-digest.lobster",
      "registered_as": "R7-1" },
    ...
  ]
}
```

`registered_as` is the cron-job-id or hook-id from the registrar,
preserved for later deregistration / introspection.

## Data Flow

```
Step 2 architect-proposal
  └─ atlas-skeleton-generator --action=draft
       ├─ existing archetype-synthesis LLM call now ALSO emits triggers[]
       │  per requirement (no extra LLM call)
       ├─ writes staging tree under {staging}/workspace/
       └─ writes {staging}/build-manifest.json including triggers[]

Step 4 vet-walk
  └─ atlas-requirement-walker presents triggers[] per requirement
       ├─ "approve" / "change schedule" / "drop trigger" / "drop requirement"
       └─ persisted into per-requirement decision in fast-io

Step 5 recap-and-cost
  └─ recap-diff now diffs triggers as well as classification + user-story

S9 architecture-sign-off agentgate (unchanged)

S9.5 NEW determinism-audit gate (hard block, no override)
  └─ atlas-determinism-audit.js asserts every deterministic req has triggers
     pointing at staged-and-existing .lobster pipelines

S10 file-write-gate (file list now includes nested paths +
                     workspace/ root + triggers.json)

Step 6 build
  ├─ ClawHavoc scan (unchanged)
  ├─ sensitive-sniff (unchanged)
  ├─ atomic write (recursive; via staging-dir-then-mv)
  ├─ verify-write (recursive SHA256)
  ├─ openclaw agents add
  ├─ NEW register-triggers (cron + hook + channel)
  ├─ widened rollback on any of agents-add or register-triggers failure
  └─ audit-log (now includes per-trigger registration result)
```

## What Stays the Same

- 6-step methodology and the seventh optional Marketplace Publish step.
- All hard rules: Research Before PRD, Vet Every Requirement, Lock
  Identity at Intake, No Invented Sources / Guessed Schemas, No
  Live-Test Claims, Anti-Overcomplication, plus citation-subclaim
  granularity.
- ClawHavoc denylist + sensitive-sniff WARN at build time and HARD at
  publish time.
- Identity lock + ATLAS1 self-protection regex.
- S10 file-write-gate, S11 marketplace-submit-gate, A14
  security-exception agentgate.
- Atlas's own `openclaw.json` and `agents/atlas/` directory layout.
- Local-host fallback via `ISOL8_AGENTS_DIR` env var.
- Marketplace publish flow and consumed-side
  `catalog_service.deploy_from_artifact`.

## Open Questions (resolve during plan writing)

**Q1. Hook registration CLI shape.** `openclaw hooks register {route}
--pipeline {pipeline}` is the placeholder. The real OpenClaw conventions
may instead be:

- (a) per-route CLI registration as drafted, OR
- (b) implicit binding by lobster pipeline declaration (the pipeline
  itself declares which route it handles, and openclaw discovers it on
  `openclaw agents add`), OR
- (c) routes declared statically in `openclaw.json#hooks` and dispatched
  by the framework with no per-route registration step.

If (b) or (c), step 6.9.5 collapses to "verify the pipeline declares its
hook trigger correctly" rather than a CLI invocation; the
`atlas-trigger-registrar.js` `hook` branch becomes a no-op assert.
Resolution: check OpenClaw docs cache at
`atlas-cookbook-reader.js`'s docs-cache root during plan-writing.

**Q2. Channel binding format.** The proposed approach writes channel
bindings into the agent's `openclaw.json#channels` block by in-place
merge. Need to confirm whether existing OpenClaw channel handling reads
a `match` / `route` field at the agent scope or only globally. If only
globally, channel triggers move out of this design and the `channel`
kind is dropped from v1.

**Q3. Cron-job-id naming.** Draft is `{req_id}-{n}` (e.g. `R7-1`). This
keeps deregister-on-rollback cheap (deterministic from manifest), but
collides across builds if two builds for the same agent share `req_id`s
— which can happen on a rebuild. The plan should pick between
`{build_id}-{req_id}-{n}` (collision-free) and `{req_id}-{n}` with a
pre-write "delete any existing cron with this id" sweep.

**Q4. Verify seed-deploy layout before Atlas mirrors it.** The
`workspace/` split is what `openclaw.json#agents.defaults.workspace`
encodes, but the actual on-EFS layout the seed-deploy creates for
Atlas/Echo/etc. is the empirical ground truth. Two possibilities:

- (a) the seed-deploy copies `agents/atlas/` flat to EFS root and the
  OpenClaw runtime accepts the agent dir as workspace when the declared
  workspace subdir doesn't exist, OR
- (b) the seed-deploy creates `~/.openclaw/agents/{name}/workspace/`
  and copies the repo's flat contents into it.

If (a), Atlas's write target should match the runtime behavior (flat),
not the declared path. If (b), Atlas's write target is the
`workspace/` subdir as drafted. Plan-writing step 1: confirm by reading
`apps/backend/core/containers/config.py:write_openclaw_config` plus
whatever seed-copy logic accompanies it, before committing to the
`workspace/` template change in section 2.

## Testing Notes

Two layers:

- **Unit:** `atlas-skeleton-generator.js`, `atlas-trigger-registrar.js`,
  and `atlas-determinism-audit.js` each get a focused test that
  exercises the success path and one representative failure per branch.
  Recursive write + nested SHA verify is the highest-value new unit
  test.
- **Integration:** A scripted build run end-to-end against a synthetic
  3-requirement spec (one deterministic with cron, one deterministic
  with hook, one llm with no triggers). Asserts:
  - Workspace tree lands at `~/.openclaw/agents/{name}/workspace/...`
  - `openclaw cron list` shows the cron entry post-build
  - Hook route is discoverable (mechanism depends on Q1 resolution)
  - Rollback path: induce an `openclaw agents add` failure and assert
    every prior cron registration is removed and no `agents/{name}/` dir
    remains

## Files Touched

- `agents/atlas/atlas-skeleton-generator.js` — recursive walk, nested
  hashes, `workspace/` root in template, `triggers[]` consumption,
  emit `triggers.json` + `build-manifest.json`.
- `agents/atlas/atlas-trigger-registrar.js` — NEW.
- `agents/atlas/atlas-determinism-audit.js` — NEW.
- `agents/atlas/build.lobster` — add `register-triggers` step, widen
  rollback condition.
- `agents/atlas/recap-and-cost.lobster` — insert S9.5 determinism-audit
  gate after S9 architecture-sign-off.
- `agents/atlas/architect-proposal.lobster` — pass `triggers[]` schema
  hint into the archetype-synthesis LLM call.
- `agents/atlas/vet-walk.lobster` — surface `triggers[]` per
  requirement to the walker.
- `agents/atlas/atlas-requirement-walker.js` — render and accept
  trigger edits per row.
- `agents/atlas/AGENTS.md` — append a section explaining the
  determinism contract and S9.5; do NOT touch the "Build Session
  Learnings" appendix (carries from prior builds).

## Visual Reference

None — this is a backend / build-pipeline change. No UI surface.
