# TOOLS.md

Reference for the skills, pipelines, scripts, and contracts Atlas relies on — the mechanism behind AGENTS.md's philosophy.

**Everything is gateway-free.** The embedded runner has no gateway (gateway tool calls 401, the exec proxy exits 127), so pipelines run real programs under bare `command: node`, durable state lives under `atlas/*` keys via native `state.get`/`state.set`, hard confirms are native `approval:` gates, and LLM work is queued onto `atlas/notifications` for the agent loop to drain.

## Pipelines (lobster)

Seven `.lobster` pipelines — five build steps + two crons:

| Pipeline | Trigger | Purpose | LLM |
|---|---|---|---|
| `intake-and-research` | webhook `/hooks/atlas-build-start` | Steps 1–2: identity lock, stack overlap (+ `approval:`), cookbook + failure-case research; brief queued | 1 (+1 cond.) |
| `architect-proposal` | from intake completion | Step 3: skeleton draft, det/LLM classifier, skill recommend; archetype synthesis queued | 1 |
| `vet-walk` | per-requirement loop | Step 4: next un-vetted row → `vet_row` intent; loop writes `atlas/decisions` | 0 |
| `recap-and-cost` | from vet completion | Step 5: SHA256 verify + diff + cost-impact + **S9 sign-off** + **S9.5 determinism audit** | 0 |
| `build` | webhook `/hooks/atlas-build-confirm` (after S9 + S9.5) | Step 6: pre-write scans + **S10 gate** + atomic write + **register via backend bridge** + triggers + rollback | 0 |
| `cookbook-refresh` | cron daily 04:00 | Diffs the OpenClaw doc index (`docs.openclaw.ai/llms.txt`), fetches changed pages, snapshots `~/.openclaw/agents/` | 0 |
| `citation-verification` | cron quarterly | Re-verifies every external citation in Atlas's + generated agents' AGENTS.md; 2 consecutive 403/404/changed → warning | 0 |

**Bridge CREATE** — the one non-gateway-free step (`build.lobster`):

```
POST /api/v1/agent-builder/u/{owner_id}/register-built-agent
Authorization: Bearer $OPENCLAW_HOOK_TOKEN
{ "staging_agent_id": "atlas-staging-<build_id>", "name": "<agent name>", "reports_to": null }
```

`staging_agent_id` is the build's **staging token** — the directory the build wrote (`workspace-atlas-staging-{build_id}/`), not a proposed agent id. Take it verbatim from `verify-write`'s `staging_agent_id`; never construct one from the agent's name. The body carries **no id field at all**: the bridge mints the agent's id and returns it. That is the only place an `agent_id` ever comes from.

`reports_to` null/omitted defaults to the company CEO (Friday) — only set it when the role spec names a different manager. The per-owner URL is substituted at stage time (placeholder `{{ISOL8_BRIDGE_URL}}`); the body is `node` `JSON.stringify`-built, piped to `curl --data @-` so a special char can't malform the JSON. The bridge mints + deploys the agent live and returns `{agent_id, registered: true, warnings: [...]}`; relay any `warnings` verbatim into the build thread (they flag e.g. a blank description → blank org-chart capabilities). `verify-write` also schema-validates the staged openclaw.json against the installed OpenClaw's own schema — any `schema_issues[]` in its result must be fixed and the build re-verified before register; never retry register while verify is failing. `build.lobster` does not publish — it ends at register + a `build_registered` notification.

## Agent loop

Each pipeline queues an intent (`kind`, `payload`, `persist_to`); the **agent loop** drains it, runs the call, writes the result back to state (~2–4 per build). Thinking: `off` for classification/extraction, `low` for synthesis; never `medium`/`high`. Pipelines never set `provider`/`model` — the platform routes per the owner's account config.

On an iteration loop (e.g. regeneration on schema-validation failure), apply the Architect/Editor split (Aider's evidence): a stronger model drafts (`thinking: low`), a cheaper one does the mechanical correction (`thinking: off`).

## State (`atlas/*` keys)

Durable per-build + operational state, read/written natively. Keys have NO interpolation — each build is namespaced **inside** the value (a map keyed by `build_id`), not in the key path; queues are plain arrays.

```
atlas/intakes[build_id]            # locked identity + scope (Step 1)
atlas/research-briefs[build_id]    # Step 1 brief (required for Step 2)
atlas/requirement-matrix[build_id] # Step 3 snapshot + matrix_sha256; synthesis merged in
atlas/decisions[build_id][req_id]  # per-req vet decision (drop-resilient resume)
atlas/vet-progress[build_id]       # Step 4 progress (decided / total / started_at)
atlas/recaps[build_id] , atlas/cost-estimates[build_id]   # Step 5 diff + cost-impact
atlas/notifications                # append queue the agent loop drains
atlas/audit-log                    # append array, capped at 500
atlas/failure-cases                # curated corpus {version, cases[]}
atlas/cookbook-docs / -agent-snapshots / -refresh-state  # cookbook cache + daily-diff state
atlas/citation-tracking            # consecutive-failure tracking (quarterly pass)
```

Built-agent files land at the EFS user root `ISOL8_AGENTS_DIR = /home/node/.openclaw`, in the build's staging workspace `workspace-atlas-staging-{build_id}/` (files under `workspace/`) — where the bridge's `export_built_agent_from_staging` reads and tars (it resolves `{user root}/workspace-{staging_agent_id}`).

Two things about that path are load-bearing. It is keyed by the **build**, never by the agent's name — the name is a display string and must never become a path or an id. And it is NOT `~/.openclaw/workspaces/`: `openclaw doctor --fix` recursively deletes that directory on every container boot, so anything staged there is gone by the next restart.

## Skills

All gateway-free (local or agent-loop-drained, never a gateway proxy):

- **`state.*`** — the runner's native durable storage, NOT a skill, for both Atlas's pipelines and the agents it emits; replaced the old gateway KV skills `fast-io`/`taskr`.
- **`agentgate`** — the underlying approval skill; Atlas's hard confirms are **native `approval:` blocks** (stack-overlap, S9, S10), with the old per-flag gates collapsed into the single S10 gate.
- **`skill-vetter`** — at bootstrap on every installed skill, and again at build time inside `atlas-skill-recommender.js` before recommending; refusals carry structured findings.
- **`sona-security-audit`** — daily background monitoring; alerts route through the agent loop.
- **`capability-evolver`** — weekly cron (Sun 06:00) reads `atlas/decisions` across builds, emits a learning summary, tunes v1-default thresholds.
- **`gog`** (Google Workspace, for user design docs), **`slack`** / **`message`** (agent-loop channels surfacing drained `atlas/notifications` intents), **`llm-task`** (the agent-loop LLM call).

`fast-io`, `summarize`, `n8n-workflow` are **not used** (gateway-bound) — replaced by `state.*`, the queued LLM synthesis, and the agent-loop re-invoke respectively.

## Deterministic Scripts

Every Node script is `stdin-JSON → stdout-JSON`, a bare `node` step, exit 0|1, structured errors on stderr. Steps set `on_error: continue` so a script abort never kills the run; downstream steps default their inputs (`JSON.parse(x||"null")`).

| Script | Purpose |
|---|---|
| `atlas-identity-lock.js` | Locks new agent name/role/scope; immutable record + SHA256; refuses reserved names (ATLAS1) |
| `atlas-stack-overlap.js` | Reads `~/.openclaw/agents/*/openclaw.json` structured fields only → `{overlap_score, conflicting_agents, recommendation}` |
| `atlas-cookbook-reader.js` | Two-source cookbook (docs cache + agent snapshots); drives the daily refresh (`diff-index`/`fetch-pages`/`snapshot-agents`) |
| `atlas-failure-case-router.js` | Filters the corpus by archetype; refuses any case whose source URL isn't `verified`; `--refresh` re-verifies the corpus |
| `atlas-skeleton-generator.js` | `draft` returns the artifact set; `write-disk` (takes `--build-id` only — no agent name) gates the promote on both the staged openclaw.json's schema AND its id being the build's staging token, then atomically materializes into `workspace-atlas-staging-{build_id}/` (nothing invalid is ever promoted, so a corrected rebuild is never blocked); `verify-write` checks count + per-file SHA256 + re-validates the schema and returns `staging_agent_id` |
| `atlas-det-llm-classifier.js` | Per-requirement deterministic / LLM / hybrid classification with rationale |
| `atlas-skill-recommender.js` | Archetype + requirements → skill list + deny list; skill-vetter first; refuses any ClawHavoc-listed skill |
| `atlas-requirement-walker.js` | Step 4 vet: `present-next`/`apply-trigger-edits`/`check-loop`; refuses to advance past an unresolved row; H6 fuzzy match |
| `atlas-recap-diff.js` | Step 5: `verify-hash` (aborts on SHA256 mismatch); `diff` emits original→final |
| `atlas-cost-counter.js` | Counts deterministic vs LLM nodes → count + ratio, "projected" never "tested" (H5); `--rollup-day` nightly rollup |
| `atlas-determinism-audit.js` | S9.5: every `deterministic` requirement must have triggers AND each trigger pipeline must exist in staging; **hard block, no override** |
| `atlas-trigger-registrar.js` | Step 6: `register` writes cron rows to the persisted shape (no CLI); `deregister` cleans this build's rows by `{build_id}-` prefix |
| `atlas-clawhavoc-enforcer.js` | Build-time HARD scan of the bundle for any `CLAWHAVOC_DENYLIST` reference |
| `atlas-sensitive-sniff.js` | Regex + entropy scan for likely keys/tokens/env values/identifiers; WARN pointers in the S10 gate; dismissal audit-logged |
| `atlas-activation-check.js` | Bootstrap validator (env vars, bridge-URL substitution, state defaults, cron, channels, security stack, denylist) |
| `atlas-citation-verifier.js` | Quarterly pass on every external citation; 2 consecutive 403/404/changed → stale-citation warning |

Hard-rule enforcement (above) must stay `Active`: H1 `architect-proposal`, H2/H6 `requirement-walker`, H3 `identity-lock`, H4 `failure-case-router` + `citation-verifier`, H5 `cost-counter`. Any drift to stubbed/bypassed requires an explicit appendix entry — silent drift is a methodology failure.

## Frozen Inventory

Frozen `Object.freeze` constants. Update discipline: append + bump version + add to this inventory + add a validator test (bumping any without the checklist surfaces as ICE on the next user-build).

| Constant (file) | Purpose |
|---|---|
| `CLAWHAVOC_DENYLIST`, `CROSS_AGENT_FLOOR` (`atlas-clawhavoc-enforcer.js`) | Skills Atlas refuses + cross-agent floor — canonical names in openclaw.json `tools.deny[]` (referenced, not inlined) |
| `COOKBOOK_SOURCE_PREFERENCE` (`atlas-cookbook-reader.js`) | Per-topic docs-vs-snapshots preference (source: `lib/analyze/cookbook-source-source.json`) |
| `STRONG_SIGNAL_TOKENS` (`atlas-requirement-walker.js`) | H6 anti-overcomplication trigger tokens |
| `MAX_PARSE_RETRIES` (`atlas-skeleton-generator.js`) | Schema-violation retry budget; default 3 (v1) |
| `THRESHOLDS` (`atlas-stack-overlap.js`) | EXTEND_FLOOR 70%, SEPARATE_CEILING 30%, AMBIGUOUS_BAND [30,70] (v1) |
| `CONSECUTIVE_FAILURE_THRESHOLD` (`atlas-citation-verifier.js`) | 2-strike stale-citation rule |
| `LLM_RATIO_WARN_THRESHOLD` (`atlas-det-llm-classifier.js`) | Projected-LLM-ratio warning; default 15% (v1) |
| `RESERVED_NAMES_PATTERN` (`atlas-identity-lock.js`) | `/^atlas(-v\d+)?$/i` (ATLAS1) |

## Verified Source Definition

The refusal message for a build-time ClawHavoc-listed-skill request links here. **Verified** = a publisher-signed manifest URL from the official domain, a signed release artifact from the publisher's GitHub releases (verifiable PGP), or a direct URL to the skill's manifest in the publisher's official docs. **Not verified** = a ClawHub community comment, a third-party repost/fork, a fork of unknown provenance, a `*/community-skills/` path, or a search-result link.

A URL claiming verification is run through `skill-vetter`; only a vetter pass makes the skill recommendable, and a denied skill installs only via the S10 gate (no separate override). The denylist is isol8's curated internal list (`tools.deny[]`), no publication cited inline.

## Atomic Write Contract

`atlas-skeleton-generator.js --action=write-disk` writes all files into `$ISOL8_AGENTS_DIR/.atlas-staging-{build_id}/`, verifies count + per-file SHA256, then `mv`s to `$ISOL8_AGENTS_DIR/workspace-atlas-staging-{build_id}/` (atomic on POSIX via `fs.renameSync`); on failure it `rm -rf`s staging and emits a structured error report. Atomicity is at the `mv` — half-atomicity is worse than none, so don't half-implement it. Both dirs sit under the same EFS root on purpose: `rename(2)` across filesystems is `EXDEV`, and `~/.openclaw/agents` is an ephemeral local mount, not EFS.

## Universal Skill Core

Every Atlas-emitted agent ships this core in its openclaw.json `skills` (must match `UNIVERSAL_SKILL_CORE` in `atlas-skeleton-generator.js`):

```
gog, slack, capability-evolver, skill-vetter, sona-security-audit, agentgate
```

(`state.*`, `llm-task`, `message`, `lobster` aren't installable skills, so they're not listed.) The generator merges the core with user-vetted additions (deduplicated); at S9 the user can reject any core skill, warned it likely yields a non-functional agent.

## Stale Build Archive Policy

Paused builds (walker idle 24h → 7d total) move to an `_archived` namespace via the `atlas-stale-vet-archiver` cron (Sun 04:00), then hard-delete after 90 days (initiator notified day 83). Per-build scratch is deleted right after Step 6's verify-write, but audit artifacts (`decisions`, `recaps`, `audit-log`) are kept 12 months. A failed register leaves an inert staging dir on disk (not in openclaw.json, so it does nothing) — v1 leaves it in place and surfaces a `build_rollback` notice, no destructive handling.
