# TOOLS.md

Reference for every skill, pipeline, Node script, and contract Atlas relies on. AGENTS.md is the operating philosophy; TOOLS.md is the implementation reference.

Everything is **gateway-free**. The embedded runner has no gateway: gateway tool calls 401 and shelling out to the gateway exec proxy exits 127. So pipelines run real programs under bare `command: node`, durable state lives under `atlas/*` keys via native `pipeline: state.get` / `state.set`, hard confirms are native `approval:` gates, and any LLM / Slack / summarize work is queued as an intent on the `atlas/notifications` state queue for the agent loop to drain.

---

## Lobster (Pipeline Orchestration)

Seven `.lobster` pipelines. Each maps to a step in the methodology or an operational cron.

| Pipeline | Trigger | Purpose | Queued LLM intents |
|---|---|---|---|
| `intake-and-research.lobster` | webhook `/hooks/atlas-build-start` | Steps 1 + 2: identity lock, stack overlap (+ native `approval:` gate), cookbook + failure-case research; research-brief synthesis queued for the agent loop → `atlas/research-briefs[build_id]` | 1 (research brief; +1 conditional overlap-disambig) |
| `architect-proposal.lobster` | from intake completion | Step 3: skeleton draft, det/LLM classifier, skill recommend; archetype synthesis queued → merged into `atlas/requirement-matrix[build_id]` | 1 (archetype synthesis) |
| `vet-walk.lobster` | per-requirement loop (agent-loop re-invokes for the next row) | Step 4: walker presents the next un-vetted row as a `vet_row` intent; the loop writes the decision into `atlas/decisions[build_id][req_id]` | 0 |
| `recap-and-cost.lobster` | from vet completion | Step 5: SHA256 matrix verify + original→final diff + deterministic-vs-LLM cost-impact + **S9 architecture sign-off** (`approval:`) + **S9.5 determinism audit** (hard, no override) | 0 |
| `build.lobster` | webhook `/hooks/atlas-build-confirm` (after S9 + S9.5) | Step 6: pre-write scans + **S10 file-write `approval:` gate** + atomic write + **register the agent via the backend bridge** (`register-built-agent`) + trigger registration + widened rollback | 0 |
| `cookbook-refresh.lobster` | cron daily 04:00 | Diffs the OpenClaw doc index from `https://docs.openclaw.ai/llms.txt`, fetches changed pages, and snapshots `~/.openclaw/agents/` into `atlas/cookbook-docs` + `atlas/cookbook-agent-snapshots` | 0 |
| `citation-verification.lobster` | cron quarterly | Verification pass on every external citation in Atlas's AGENTS.md + any Atlas-generated agents' AGENTS.md; 2 consecutive 403/404/changed-content → stale-citation warning intent | 0 |

The CREATE step in `build.lobster` is the one thing Atlas cannot do gateway-free in-container, so it curls the backend bridge:

```
POST /api/v1/agent-builder/u/{owner_id}/register-built-agent
Authorization: Bearer $OPENCLAW_HOOK_TOKEN
Content-Type: application/json
{ "staging_agent_id": "<on-disk staging dir name>", "name": "<agent name>", "reports_to": null }
```

The bridge snapshots `workspaces/{staging_agent_id}/`, mints a Paperclip agent, deploys it live, and returns `{agent_id, registered: true}`. The per-owner URL is substituted into `build.lobster` at stage time (vendored placeholder `{{ISOL8_BRIDGE_URL}}`). The body is built with `node` (`JSON.stringify`) and piped to `curl --data @-` so an agent name containing a quote/backslash/newline can't malform the JSON or split the curl args. **`build.lobster` does not publish** — it ends at register + a `build_registered` notification carrying the `agent_id` for the owner to publish via the seller wizard.

---

## llm-task (agent loop)

LLM work has no gateway-free equivalent inside a pipeline, so each pipeline queues an intent (with a `kind`, `payload`, and `persist_to` key) onto `atlas/notifications`. The **agent loop** — Atlas's own reasoning loop — drains the queue, runs the LLM call via `llm-task`, and writes the result back into state (e.g. the research brief lands at `atlas/research-briefs[build_id]`; archetype synthesis merges into `atlas/requirement-matrix[build_id]`). A typical build queues ~2–4 such intents.

### Architect/Editor pairing (where it applies)

When an iteration loop fires (e.g., regeneration on schema-validation failure), the Architect/Editor split per Aider's published evidence: stronger model on the Architect (drafter) call, cheaper model on the Editor (mechanical correction) call. Pulse's April 2026 live test data informs the asymmetry — drafting requires more model power; reviewing/rule-application doesn't. The loop requests them by capability (`thinking: low` for the drafter, `thinking: off` for the editor) and lets the platform choose actual models. Pipelines never specify `provider`/`model`; the platform routes per the user's account configuration.

### Thinking levels

`off` for classification/extraction. `low` for synthesis (research brief, archetype synthesis). Never `medium` or `high`.

---

## State (durable storage)

Durable per-build and operational state lives under `atlas/*` keys, read/written natively with `pipeline: state.get` / `state.set`. State keys are plain strings with NO interpolation, so each build is namespaced **inside** the value (a map keyed by `build_id`), not in the key path. The queues are plain arrays.

```
atlas/intakes[build_id]                  # locked identity + scope from Step 1
atlas/research-briefs[build_id]          # Step 1 brief (agent-loop write; required for Step 2)
atlas/requirement-matrix[build_id]       # Step 3 snapshot (+ embedded matrix_sha256) → agent-loop merges synthesis
atlas/decisions[build_id][req_id]        # per-requirement vet decision (drop-resilient resume)
atlas/vet-progress[build_id]             # Step 4 progress record (decided / total / started_at)
atlas/recaps[build_id]                   # Step 5 diff
atlas/cost-estimates[build_id]           # Step 5 cost-impact output
atlas/notifications                      # append queue of intents the agent loop drains
atlas/audit-log                          # append array, capped at 500 entries
atlas/failure-cases                      # curated corpus {version, cases[]}
atlas/cookbook-docs                      # cached OpenClaw doc pages (refreshed daily)
atlas/cookbook-agent-snapshots           # snapshots of prior builds {count, agents[]}
atlas/cookbook-refresh-state             # last_refreshed + indexed_urls (for the daily diff)
atlas/citation-tracking                  # consecutive-failure tracking for the quarterly pass
```

The built agent's files are materialized to the EFS workspaces root (`ISOL8_AGENTS_DIR` = `/home/node/.openclaw/workspaces`), landing at `workspaces/{agent_name}/` with files under `workspace/` — exactly where the bridge's `export_built_agent_from_staging` reads and tars.

---

## Skills

The skills Atlas genuinely uses, all gateway-free (run as local skills / drained by the agent loop — never via a gateway tool proxy).

### taskr (state-backed KV)

`taskr` is the gateway-free KV skill (the fast-io replacement). Atlas's own pipelines use native `state.get`/`state.set` under `atlas/*`; emitted agents use `taskr` as their durable per-key store. There is no gateway tool-call surface for it anymore — that path 401s gateway-free.

### agentgate (approval backing)

`agentgate` remains installed as the underlying approval skill (skill-vetter pre-install required; installation reference: Ember's BOOTSTRAP.md). But Atlas's hard confirms are **native `approval:` blocks** in the pipelines, not gateway tool calls. The native gates fire at three points:

1. Stack-overlap surface (Step 1) — before research begins
2. Architecture sign-off (S9, end of Step 5) — before Step 6 file generation
3. File-write + register (S10, start of Step 6) — presents scan flags + file list + bundle SHA256; ALL file work and the bridge CREATE gate on its approval

The old per-flag agentgates (A14 build-time security exception, sensitive-data dismissal) collapse into the single authoritative S10 gate — the human sees the scan flags in the prompt and approves the whole write.

### Other skills (brief)

- `skill-vetter` — runs at bootstrap on every installed skill; runs again at build-time inside `atlas-skill-recommender.js` before any skill is recommended; refusal includes the vetter's structured findings
- `sona-security-audit` — runtime monitoring of installed skills; daily background; surfaces alerts the agent loop routes to the status channel
- `capability-evolver` — weekly cron (Sun 06:00) reads `atlas/decisions` across builds and emits a learning summary; tunes v1-default thresholds
- `gog` — Google Workspace; used for any user-supplied design docs; optional
- `slack` — agent-loop channel for vet rows + sign-off prompts + build outcomes (the loop posts the drained notifications here)
- `message` — agent-loop messaging primitive for surfacing intents drained from `atlas/notifications`
- `llm-task` — agent-loop LLM call (see the llm-task section above)

`fast-io`, `summarize`, and `n8n-workflow` are **not used** — they were gateway-bound (fast-io is replaced by native `state.*` + taskr; n8n self-triggering is replaced by the agent loop re-invoking the next pipeline run; summarize had no gateway-free equivalent and its compression role folded into the queued LLM brief synthesis). They 401 gateway-free, so nothing depends on them.

---

## Deterministic Scripts (local, stdin-JSON → stdout-JSON)

Every Node script follows the `stdin-JSON → stdout-JSON` convention and runs as a bare `node` step. Shared `parseArgs` + `readStdin` patterns; structured errors on stderr; exit code 0|1. Steps set `on_error: continue` so a script abort never kills the run; downstream node steps default their inputs (`JSON.parse(x||"null")`).

| Script | Purpose |
|---|---|
| `atlas-identity-lock.js` | Locks new agent's name/role/scope at intake; emits immutable identity record with SHA256; refuses reserved names per ATLAS1 regex `/^atlas(-v\d+)?$/i` |
| `atlas-stack-overlap.js` | Reads `~/.openclaw/agents/*/openclaw.json` structured fields only (never AGENTS.md prose); returns `{overlap_score: 0-100, conflicting_agents, recommendation: "extend"|"separate"|"no_overlap"|"ambiguous"}` |
| `atlas-cookbook-reader.js` | Two-source cookbook (OpenClaw docs cache + agent snapshots); also drives the daily refresh actions (`diff-index`, `fetch-pages`, `snapshot-agents`) for `cookbook-refresh.lobster` |
| `atlas-failure-case-router.js` | Filters the failure-case corpus by archetype tag; refuses to inject any case whose source URL isn't marked `verified`; `--refresh` re-runs corpus verification (twice-weekly cron) |
| `atlas-skeleton-generator.js` | `--action=draft` returns the artifact set; `--action=write-disk` atomically materializes the agent into `workspaces/{agent_name}/` (staging-dir-then-mv); `--action=verify-write` checks file count + per-file SHA256 |
| `atlas-det-llm-classifier.js` | Per-requirement classification deterministic / LLM / hybrid with rationale |
| `atlas-skill-recommender.js` | Given archetype + requirements, emits a recommended skill list with reasoning + deny list; runs skill-vetter as a sub-process before any skill is recommended; refuses any ClawHavoc-listed skill universally |
| `atlas-requirement-walker.js` | Drives Step 4 vet: `--action=present-next` surfaces the next un-vetted row; `--action=apply-trigger-edits` normalizes the agent-loop's decision before it's written to `atlas/decisions`; `--action=check-loop` does the completed/total bookkeeping; refuses to advance past an unresolved row; H6 anti-overcomplication via fuzzy match |
| `atlas-recap-diff.js` | Step 5: `--action=verify-hash` re-verifies the matrix SHA256 (aborts on mismatch); `--action=diff` emits the original→final diff table |
| `atlas-cost-counter.js` | Counts deterministic vs LLM nodes; emits architectural-replacement count and ratio labeled "projected" never "tested" per H5; `--rollup-day` does the nightly audit-log rollup |
| `atlas-determinism-audit.js` | S9.5: asserts every `deterministic`-classified requirement has triggers AND each trigger's pipeline exists in staging; reports pass/fail on stdout and never halts (so the failure notice can run); **hard block, no override** |
| `atlas-trigger-registrar.js` | Step 6: `--mode=register` writes the built agent's cron rows directly to the persisted cron-jobs shape (no `openclaw cron add` CLI); `--mode=deregister` cleans this build's rows by `{build_id}-` name prefix on rollback |
| `atlas-clawhavoc-enforcer.js` | Scans the bundle (openclaw.json + workspace .md + .lobster + .js) for any reference to `CLAWHAVOC_DENYLIST` (frozen `Object.freeze()` constant); build-time HARD scan (the security override is folded into the S10 gate, not a separate agentgate) |
| `atlas-sensitive-sniff.js` | Regex + entropy scan for likely API keys, OAuth tokens, env-var values, hardcoded user identifiers; returns warnings (not blocks) with line-level pointers surfaced in the S10 gate prompt; dismissal audit-logged with structured `{type, line, matched_pattern}` |
| `atlas-activation-check.js` | Bootstrap validator (env var, bridge-URL substituted, state defaults, cron, channels, security stack, ClawHavoc denylist completeness) |
| `atlas-citation-verifier.js` | Quarterly pass on every external citation in Atlas's AGENTS.md and any Atlas-generated agents' AGENTS.md; on 2 consecutive 403/404/changed-content responses, queues a stale-citation warning |

---

## Frozen Inventory Index

All frozen `Object.freeze` constants Atlas maintains, with single update discipline (append + bump version + add to inventory).

| Constant | File | Purpose |
|---|---|---|
| `CLAWHAVOC_DENYLIST` | `atlas-clawhavoc-enforcer.js` | Skills Atlas refuses to recommend or include (canonical list lives in openclaw.json `tools.deny` array — TOOLS.md references that file as source of truth, no inline duplication) |
| `CROSS_AGENT_FLOOR` | `atlas-clawhavoc-enforcer.js` | Cross-agent denylist floor (also referenced in openclaw.json `tools.deny`) |
| `COOKBOOK_SOURCE_PREFERENCE` | `atlas-cookbook-reader.js` | Per-topic preference for OpenClaw docs vs agent snapshots |
| `STRONG_SIGNAL_TOKENS` | `atlas-requirement-walker.js` | H6 anti-overcomplication trigger tokens |
| `MAX_PARSE_RETRIES` | `atlas-skeleton-generator.js` | Retry budget on schema-violation; default 3, tagged v1 default |
| `THRESHOLDS` (stack-overlap) | `atlas-stack-overlap.js` | EXTEND_FLOOR (70%), SEPARATE_CEILING (30%), AMBIGUOUS_BAND ([30, 70]); v1 defaults |
| `CONSECUTIVE_FAILURE_THRESHOLD` | `atlas-citation-verifier.js` | 2-strike rule for stale-citation warning |
| `LLM_RATIO_WARN_THRESHOLD` | `atlas-det-llm-classifier.js` | Projected-LLM-ratio warning threshold; default 15%, v1 default |
| `RESERVED_NAMES_PATTERN` | `atlas-identity-lock.js` | `/^atlas(-v\d+)?$/i` — names Atlas refuses to use (ATLAS1) |

Update process: append entries + bump version + add to this inventory + add validator test case. Bumping any frozen constant without following the checklist surfaces as ICE on next user-build.

---

## Hard Rule Enforcement Map

| Rule | Enforcement file path | Status |
|---|---|---|
| H1: Research Before PRD, Always | `architect-proposal.lobster` (missing-brief path defers when `atlas/research-briefs[build_id]` is absent) | Active |
| H2: Vet Every Requirement, No Skipping | `atlas-requirement-walker.js` (refuses advance past unresolved row) | Active |
| H3: Lock Identity at Intake | `atlas-identity-lock.js` (immutable record + SHA256 + ATLAS1 regex) | Active |
| H4: No Invented Sources, No Guessed Schemas (subclaim granularity) | `atlas-failure-case-router.js` (refuses unverified-source cases); `atlas-citation-verifier.js` (quarterly check); citation discipline applies in user-facing error templates | Active |
| H5: No Live-Test Claims Without a Live Test | `atlas-cost-counter.js` (outputs labeled "projected" never "tested") | Active |
| H6: Anti-Overcomplication | `atlas-requirement-walker.js` (STRONG_SIGNAL_TOKENS fuzzy match + collapse with explicit scope enumeration) | Active |

Future contributors: any drift from `Active` to `Stubbed` or `Bypassed-with-Justification` requires explicit BSL or Frame Correction appendix entry. Silent drift is a methodology failure — surface it.

---

## Citation Discipline

Two rules from accumulated catches.

**1. Subclaim granularity (BSL catch #6 generalization).** Confirming a citation exists is not sufficient — each specific subclaim cited from it needs independent verification against the source content. A real paper that doesn't make the specific claim being attributed to it = drop the subclaim, keep the verified portion.

**2. Atlas-side facts vs external context (P2.3 meta-pattern).** When citing external sources in user-facing error messages, separate Atlas-verified facts from external supporting context. Don't let citation framing inflate Atlas's verifiable claim into a stronger claim the citation doesn't actually support.

Example (the P2.3 dependency-error case):

> "Package `react-codeshift` not found on npm and not in Atlas's verified dependency whitelist. Unverified package names are commonly LLM hallucinations (Spracklen et al., arxiv 2406.10279, documented 205,474 such hallucinations across studied LLMs). Did you mean: jscodeshift, react-codemod?"

Two facts cleanly separated:
- **Atlas-side verified fact:** "package not on npm and not in whitelist" — Atlas confirms via npm registry API call (`npm view {{name}}` returns non-zero) AND whitelist lookup
- **General supporting context:** Spracklen et al. cited as evidence that LLM hallucination is a documented phenomenon at scale, NOT as a per-package accusation

When npm registry is unreachable: error message says "package not in Atlas's verified whitelist; npm registry unreachable to confirm absence" rather than implying npm has confirmed absence.

---

## Stale Build Archive Policy

All accumulating-state lifecycles consolidated. Single retention discipline document.

| Policy | Trigger | Retention | Cleanup mechanism |
|---|---|---|---|
| Paused build → Archive | Walker idle 24h → 7d total inactivity | Move the build's per-build state entries to an `_archived` namespace | `atlas-stale-vet-archiver` cron Sun 04:00 |
| Archived build → Hard-delete | 90 days in archive | Drop the archived entries; build_initiator notified on day 83 with recovery option | Same cron, single weekly walk handles all stages |
| Inert staging dir (failed-build artifact) | A failed register leaves `workspaces/{staging_agent_id}/` on disk, not listed in openclaw.json (so it does nothing) | Surface a notice; v1 leaves the inert dir in place (no destructive path handling in the pipeline) | None in v1 — surfaces a `build_rollback` notification |
| Per-build-id scratch | After Step 6 atomic mv complete + verify-write | Cleanup the staging dir immediately; preserve audit artifacts (`atlas/decisions`, `atlas/recaps`, `atlas/audit-log`) for 12 months | Same cron handles the 12-month boundary |

---

## Click-to-Connect Integrations

Atlas does not interrogate the user about integrations during onboarding. Click-to-connect skills are declared `enabled: false` in openclaw.json with explanatory `note`; Atlas only references them during the Step 1 stack-overlap check.

When a user requests a build that needs a skill they haven't connected:

> "To do <X>, connect <service> in your isol8 settings. Supported: <option1>, <option2>, <option3>. You can connect more than one."

Never asked during onboarding — only when needed.

---

## Atomic Write Contract

`atlas-skeleton-generator.js`'s `--action=write-disk` follows the staging-dir-then-mv pattern. Filesystem-level atomicity at the `mv` step. `ISOL8_AGENTS_DIR` points at the EFS workspaces root, so the built agent lands at `workspaces/{agent_name}/`.

```
1. Draft artifacts come in on stdin (from --action=draft).
2. Disk write step:
   a. mkdir $ISOL8_AGENTS_DIR/.atlas-staging-{{build_id}}/
   b. Write all artifact files to the staging dir
   c. Verify file count + per-file SHA256 against the staged artifact hashes
   d. On success: mv $ISOL8_AGENTS_DIR/.atlas-staging-{{build_id}}/ $ISOL8_AGENTS_DIR/{{agent_name}}/  (atomic on POSIX/local FS via fs.renameSync)
   e. On any failure: rm -rf the staging dir + a structured error report listing which file failed, the staged artifact hashes, build_id, staging path, and destination path
```

The bridge then reads `workspaces/{agent_name}/` and tars it. No two-phase commit, no transaction semantics — just staging-dir-and-mv at the filesystem layer. Documented explicitly so future contributors don't half-implement: half-atomicity is worse than no atomicity.

---

## Verified Source Definition

Required reading for any user requesting installation of a ClawHavoc-listed skill at build time (the refusal message links here).

**A verified source is:**
- A publisher-signed manifest URL from the official source domain
- A signed release artifact from the publisher's GitHub releases page with verifiable PGP signature
- A direct URL to the skill's manifest in the publisher's official documentation

**A verified source is NOT:**
- A ClawHub community comment recommending the skill
- A third-party repost or fork of the skill manifest
- A GitHub fork of unknown provenance
- A direct link to a skill on ClawHub's `*/community-skills/` path
- A search-result link

**To obtain a verified URL:** check the publisher's official documentation; look for signed releases on the publisher's GitHub; contact the publisher directly via verified channels.

When a user provides a URL claiming verification, `atlas-skill-recommender.js` runs `skill-vetter` against it as a sub-process before reconsidering. Only on a vetter pass does the skill become recommendable. A build-time install of a denied skill is approved through the S10 file-write gate (the human sees it flagged and approves the whole write); there is no separate override path.

---

## ClawHavoc Denylist Provenance

The canonical denylist is isol8's curated list, maintained internally based on public security research. Lives as a frozen `Object.freeze`d exported constant `CLAWHAVOC_DENYLIST` at the top of `atlas-clawhavoc-enforcer.js` — single-file update if the list ever changes.

The canonical names are in `openclaw.json` `tools.deny[]`. TOOLS.md references that file rather than inlining the names — keeps documentation context cleanly separated from the enforcer's scan surface.

No specific publication cited inline. During Atlas's bootstrap vet, a Trend Micro Feb 2026 attribution was proposed as an inline citation in refusal messages; verification attempt returned HTTP 403; per H4 the citation could not ship. See AGENTS.md "Build Session Learnings" appendix for the worked example.

---

## Cookbook Source Preference Table

Frozen exported constant `COOKBOOK_SOURCE_PREFERENCE` in `atlas-cookbook-reader.js`. Source-of-truth at `lib/analyze/cookbook-source-source.json`. Update process documented above in Frozen Inventory Index.

---

## Universal Skill Core

The gateway-free core every Atlas-emitted agent ships with (the standard isol8 agent shape):

```
gog, slack, taskr, capability-evolver, skill-vetter, sona-security-audit, llm-task, message
```

Durable storage is native `state.*` (with `taskr` as the per-key store); there is no `fast-io`, `summarize`, or `n8n-workflow` in the core — those were gateway-bound and 401 gateway-free, so they were dropped when the fleet moved gateway-free. The generator merges this core with the user-vetted skill additions, deduplicated. At S9 the user can selectively reject any core skill via the same selective-reject mechanism, with an explicit warning ("rejecting the core likely produces a non-functional agent that won't integrate with isol8's standard tooling").
