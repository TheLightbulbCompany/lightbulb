# TOOLS.md

Reference for every skill, pipeline, Node script, and contract Atlas relies on. AGENTS.md is the operating philosophy; TOOLS.md is the implementation reference.

---

## Lobster (Pipeline Orchestration)

Eight `.lobster` pipelines. Each maps 1:1 to a step in the methodology or an operational concern.

| Pipeline | Trigger | Purpose | LLM calls |
|---|---|---|---|
| `intake-and-research.lobster` | webhook `/hooks/atlas-build-start` | Steps 1 + 2: identity lock, stack overlap, cookbook + failure-case research, brief synthesis | 1–2 (overlap conditional + research) |
| `architect-proposal.lobster` | from intake completion | Step 3: skeleton, classifier, skill recommend, archetype synthesis | 1 |
| `vet-walk.lobster` | per-requirement loop | Step 4: walker iterates matrix, presents each row, persists decision after each gate confirmation | 0 |
| `recap-and-cost.lobster` | from vet completion | Step 5: original→final diff + deterministic-vs-LLM cost-impact + S9 architecture sign-off agentgate | 0 |
| `build.lobster` | webhook `/hooks/atlas-build-confirm` (after S9) | Step 6: pre-write scans + S10 file-write agentgate + atomic write + openclaw agents add + rollback on failure | 0 |
| `publish.lobster` | webhook `/hooks/atlas-publish-confirm` (Step 7 opt-in) | Step 7: clawhavoc HARD + sensitive-sniff WARN + listing build + S11 + marketplace-submit stub | 0 |
| `cookbook-refresh.lobster` | cron daily 04:00 | Refreshes OpenClaw doc cache from `https://docs.openclaw.ai/llms.txt` index + agent snapshots | 0 |
| `citation-verification.lobster` | cron quarterly | Quarterly verification pass on every external citation in Atlas's AGENTS.md and any Atlas-generated agents' AGENTS.md; 2 consecutive 403/404/changed-content → stale-citation warning | 0 |

---

## llm-task

Atlas uses 1–4 llm-task calls per build (typical 2.4 calls/build). Pipelines call `llm-task` without specifying `provider` or `model` — the platform routes per the user's account configuration (Atlas does not read user-account or billing config). Defaults from `openclaw.json` `plugins.llm-task.config` apply when the platform doesn't override.

### Architect/Editor pairing (where it applies)

When an iteration loop fires (e.g., re-vet on user correction, regeneration on schema-validation failure), the Architect/Editor split per Aider's published evidence: stronger model on the Architect (drafter) call, cheaper model on the Editor (mechanical correction) call. Pulse's April 2026 live test data informs the asymmetry — drafting requires more model power; reviewing/rule-application doesn't. Where Atlas needs both, it requests them by capability (`thinking: low` for the drafter, `thinking: off` for the editor) and lets the platform choose actual models.

### Thinking levels

`off` for classification/extraction. `low` for synthesis (research brief, archetype synthesis). Never `medium` or `high` in automated pipelines.

---

## fast-io (Persistent Storage)

```
atlas-config/
  identity-locks/{{build_id}}             # immutable identity records {build_id, name, role, scope, locked_at, sha256}
  cookbook-refresh-state                   # last-refreshed timestamps per cookbook source

atlas-cookbook/
  openclaw-docs/{{path}}                  # cached OpenClaw doc pages (refreshed daily)
  agent-snapshots/{{agent_name}}          # snapshots of prior builds at intake time
  failure-cases.json                      # curated corpus, refreshed Mon + Thu
  user-resolutions/{{topic}}              # user answers to D3 ask-flow gaps; checked first on next query of same topic

atlas-builds/
  {{build_id}}/intake                     # locked identity + scope from Step 1
  {{build_id}}/research-brief             # Step 1 output (required for Step 2)
  {{build_id}}/skeleton                   # Step 2 deterministic scaffold
  {{build_id}}/requirement-matrix         # Step 3 immutable snapshot — write once
  {{build_id}}/requirement-matrix.sha256  # SHA256 of requirement-matrix at snapshot time
  {{build_id}}/decisions/{{req_id}}       # per-requirement vet decision (drop-resilient resume)
  {{build_id}}/recap                      # Step 5 diff
  {{build_id}}/cost-estimate              # Step 5 cost-impact output
  {{build_id}}/artifacts/{{filename}}     # Step 6 staged file content (drafts) — drafts ≠ disk files

atlas-pending-submits/{{timestamp}}        # Step 7 marketplace submit stubs

audit/{{date}}/{{action}}                  # standard audit pattern, 12-month retention
```

---

## Skills

### taskr (ClawHub community skill)

Invocation surface verified from real usage:

```
openclaw.invoke --tool taskr --action {create|read|update} --args-json '{
  "key": "namespaced-string/{{id}}",
  "data": { ... arbitrary payload ... }
}'
```

For Atlas's vet flow: per-requirement taskr entries created at vet start with `status: pending`, updated to `status: approved|changed|dropped` as walker advances. Complements the fast-io decisions log by providing the "what's outstanding right now" view.

### agentgate (ClawHub community skill)

Installation reference: Ember's BOOTSTRAP.md. skill-vetter pre-install required. Used at four points in Atlas:

1. Architecture sign-off (S9, end of Step 5) — sync hard block before Step 6 file generation
2. Final file write (S10, start of Step 6 atomic mv) — sync hard block, presents file list + bundle SHA256
3. Marketplace submit (S11, end of Step 7) — sync hard block, presents listing + bundle SHA256 for re-confirmation against S10 hash
4. Security exception requests during build (A14) — sync hard block when user requests denied skill with verified-publisher manifest URL

### Other skills (brief)

- `fast-io` — persistent K/V storage; key schema above
- `summarize` — no-LLM compression CLI; used in Step 1 to compress cookbook reads into tractable research-brief input
- `n8n-workflow` — webhook glue for `/hooks/atlas-*` endpoints
- `capability-evolver` — weekly cron (Sun 06:00) reads `atlas-builds/*/decisions/*` and emits learning summary; tunes v1-default thresholds
- `skill-vetter` — runs at bootstrap on every installed skill; runs again at build-time inside `atlas-skill-recommender.js` before any skill is recommended; refusal includes vetter's structured findings
- `sona-security-audit` — runtime monitoring of installed skills; daily background; surfaces alerts via Slack
- `gog` — Google Workspace; used for any user-supplied design docs; optional
- `slack` — primary user interface for vet flow + agentgate confirmations

---

## Deterministic Scripts (local, stdin-JSON → stdout-JSON)

Every Node script follows the `stdin-JSON → stdout-JSON` convention. Shared `parseArgs` + `readStdin` patterns; structured errors on stderr; exit code 0|1.

| Script | Purpose |
|---|---|
| `atlas-identity-lock.js` | Locks new agent's name/role/scope at intake; emits immutable identity record with SHA256; refuses reserved names per ATLAS1 regex `/^atlas(-v\d+)?$/i` |
| `atlas-stack-overlap.js` | Reads `~/.openclaw/agents/*/openclaw.json` structured fields only (never AGENTS.md prose); returns `{overlap_score: 0-100, conflicting_agents, recommendation: "extend"|"separate"|"no_overlap"|"ambiguous"}` |
| `atlas-cookbook-reader.js` | Reads from two-source cookbook (OpenClaw docs cache + agent snapshots); applies frozen source-preference table |
| `atlas-failure-case-router.js` | Filters failure-case corpus by archetype tag; refuses to inject any case whose source URL isn't marked `verified` in the corpus entry |
| `atlas-skeleton-generator.js` | Generates 6 .md files + openclaw.json + .lobster pipeline files + .js script files for new agents from analog templates + vetted requirements; atomic write via staging-dir-then-mv |
| `atlas-det-llm-classifier.js` | Per-requirement classification deterministic / LLM / hybrid with rationale |
| `atlas-skill-recommender.js` | Given archetype + requirements, emits recommended skill list with reasoning + deny list; runs skill-vetter as sub-process before any skill is recommended; refuses any ClawHavoc-listed skill universally |
| `atlas-requirement-walker.js` | Drives Step 4 vet loop; persists per-requirement state in fast-io after every gate confirmation (drop-resilient resume); refuses to advance past unresolved row; H6 anti-overcomplication signal-detection via fuzzy match |
| `atlas-recap-diff.js` | Step 5: reads original requirement matrix (verifies SHA256, aborts on mismatch) and final decisions; emits diff table |
| `atlas-cost-counter.js` | Counts deterministic vs LLM nodes per pipeline; emits architectural-replacement count and ratio; outputs labeled "projected" never "tested" per H5 |
| `atlas-clawhavoc-enforcer.js` | Scans bundle (openclaw.json + workspace .md + .lobster + .js) for any reference to `CLAWHAVOC_DENYLIST` (frozen exported constant via `Object.freeze()`); used at build-time and publish-time with asymmetric override semantics (build-time override available via security-exception agentgate; publish-time none — Marketplace Boundary Contract) |
| `atlas-sensitive-sniff.js` | Regex + entropy scan for likely API keys, OAuth tokens, env-var values, hardcoded user identifiers; returns warnings (not blocks) with line-level pointers; user override audit-logged with structured `{type, line, matched_pattern}` per dismissed warning |
| `atlas-listing-builder.js` | Step 7: zero-LLM deterministic extraction from already-vetted artifacts — listing card cannot drift from vetted state |
| `atlas-marketplace-submit.js` | Step 7 stubbed: writes payload to `fast-io: atlas-pending-submits/{{timestamp}}` + local preview file; TODO comment lists what real API needs (auth, bundle upload, listing metadata schema, status callback); one-time migration on real-API ship surfaces accumulated stubs |
| `atlas-activation-check.js` | Bootstrap validator (env vars, fast-io defaults, cron, channels, security stack, ClawHavoc denylist completeness) |
| `atlas-citation-verifier.js` | Quarterly cron pass on every external citation in Atlas's AGENTS.md and any Atlas-generated agents' AGENTS.md; on 2 consecutive 403/404/changed-content responses, surfaces stale-citation warning via Slack |

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
| H1: Research Before PRD, Always | `architect-proposal.lobster` (reads `atlas-builds/{{build_id}}/research-brief` as required input) | Active |
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

## Stale Vet Archive Policy

All accumulating-state lifecycles consolidated. Single retention discipline document.

| Policy | Trigger | Retention | Cleanup mechanism |
|---|---|---|---|
| Paused build → Archive | Walker idle 24h → 7d total inactivity | Move to `atlas-builds/_archived/{{build_id}}/` | `atlas-stale-vet-archiver` cron Sun 04:00 |
| Archived build → Hard-delete | 90 days in archive | `rm -rf atlas-builds/_archived/{{build_id}}/`; build_initiator notified on day 83 with recovery option | Same cron, single weekly walk handles all stages |
| Trash accumulation (rollback artifacts from openclaw agents delete) | Threshold: `TRASH_ACCUMULATION_WARN_THRESHOLD` (frozen, default 100, v1 default revisit) | User-explicit-action only in v1; v1.1 will add `atlas-trash-cleanup` cron | None in v1 — surfaces warning |
| Per-build-id scratch | After Step 6 atomic mv complete + verifyWrite | Cleanup `atlas-builds/{{build_id}}/staging/` immediately; preserve audit artifacts (decisions/, mutations.json, recap.json, audit/) for 12 months per MW5 | Same cron handles 12-month boundary |

---

## Click-to-Connect Integrations

Atlas does not interrogate the user about integrations during onboarding. Click-to-connect skills are declared `enabled: false` in openclaw.json with explanatory `note`; Atlas only references them during the Step 1 stack-overlap check.

When a user requests a build that needs a skill they haven't connected:

> "To do <X>, connect <service> in your isol8 settings. Supported: <option1>, <option2>, <option3>. You can connect more than one."

Never asked during onboarding — only when needed.

---

## Atomic Write Contract

`atlas-skeleton-generator.js`'s `writeDisk()` follows staging-dir-then-mv pattern. Filesystem-level atomicity at the `mv` step.

```
1. All file content already in fast-io at atlas-builds/{{build_id}}/artifacts/{{filename}} (staged during Steps 2-6)
2. Disk write step:
   a. mkdir ~/.openclaw/agents/.atlas-staging-{{build_id}}/
   b. Write all artifact files to staging dir
   c. Verify file count + per-file SHA256 against staged artifact hash in fast-io
   d. On success: mv ~/.openclaw/agents/.atlas-staging-{{build_id}}/ ~/.openclaw/agents/{{agent_name}}/  (atomic on POSIX/local FS via fs.renameSync)
   e. On any failure: rm -rf ~/.openclaw/agents/.atlas-staging-{{build_id}}/ + structured error report listing which file failed AND including the staged artifact hashes from fast-io (so debugging doesn't require re-deriving expected content) + build_id + staging path + destination path
```

No two-phase commit, no fast-io transaction semantics — just staging-dir-and-mv at the filesystem layer. Documented explicitly so future contributors don't half-implement: half-atomicity is worse than no atomicity.

---

## Marketplace Boundary Contract

Three no-override hard refuses at the marketplace boundary, treated as a single named policy:

1. **ClawHavoc denylist on publish:** `atlas-clawhavoc-enforcer.js` HARD BLOCK with no override path
2. **Bundle continuity divergence:** any divergence between S10 file-write SHA256 and S11 marketplace-submit SHA256 → HARD REFUSE (publish.lobster aborts, not warning)
3. **ClawHavoc skill in published bundle:** same enforcer, same no-override posture

Three places where public-exposure events are hard-refused with no override. One policy. Documented as the Marketplace Boundary Contract so it reads as one thing not three coincidences.

### Asymmetric Override Semantics (build-time vs publish-time)

The build-time vs publish-time override asymmetry is intentional:

- **Build-time:** security-exception agentgate (A14) allows override after explicit user confirmation + verified-publisher URL + skill-vetter pass. Rationale: the user is configuring an agent in their own environment; if they have legitimate reason to install a denied skill (legacy data migration, vendor requirement) and can produce verified provenance, that's a knowing, audit-logged choice in a non-public-exposure context.
- **Publish-time:** no override path. Marketplace submission is a public-exposure event with different risk class — buyers downloading the agent inherit any denied-skill dependency without the same agent-builder context.

Future contributors: do NOT "fix" this inconsistency. The asymmetry is the policy.

---

## Verified Source Definition

Required reading for any user requesting installation of a ClawHavoc-listed skill (refusal message links here).

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

When a user provides a URL claiming verification, `atlas-skill-recommender.js` runs `skill-vetter` against it as a sub-process before reconsidering. Only on vetter pass does the skill become recommendable.

---

## Marketplace Submit Stub

`atlas-marketplace-submit.js` is the v1 stub. Behavior:

1. Writes payload to `fast-io: atlas-pending-submits/{{timestamp}}`
2. Writes local preview file at `~/.openclaw/atlas/preview-submits/{{timestamp}}.json`
3. Surfaces to user: *"Marketplace submit queued at {{timestamp}}. Real API not yet available; preview file at ~/.openclaw/atlas/preview-submits/{{timestamp}}.json shows exactly what would have been submitted."*

### TODO when real API ships
- Auth (per-user token vs OAuth?)
- Bundle upload (multipart vs signed URL?)
- Listing metadata schema (does it match `atlas-listing-builder.js` output?)
- Status callback (webhook vs polling?)

### One-time migration on real-API ship
When the stub is replaced, ALSO run a one-time migration: scan `atlas-pending-submits/*` for accumulated stubs and surface *"You have N pending submits from before the marketplace launched — review and submit?"* to the user. Documented as a launch-time obligation so stubbed submits don't become silent dead weight.

`pending-submits-prompt` cron (weekly Mon 09:00) reminds the user of accumulated stubs in the meantime.

---

## ClawHavoc Denylist Provenance

The canonical denylist is isol8's curated list, maintained internally based on public security research. Lives as a frozen `Object.freeze`d exported constant `CLAWHAVOC_DENYLIST` at the top of `atlas-clawhavoc-enforcer.js` — single-file update if the list ever changes.

The canonical names are in `openclaw.json` `tools.deny[]`. TOOLS.md references that file rather than inlining the names — keeps documentation context cleanly separated from the enforcer's scan surface.

No specific publication cited inline. During Atlas's bootstrap vet, a Trend Micro Feb 2026 attribution was proposed as inline citation in refusal messages; verification attempt returned HTTP 403; per H4 the citation could not ship. See AGENTS.md "Build Session Learnings" appendix for the worked example.

---

## Cookbook Source Preference Table

Frozen exported constant `COOKBOOK_SOURCE_PREFERENCE` in `atlas-cookbook-reader.js`. Source-of-truth at `lib/analyze/cookbook-source-source.json`. Update process documented above in Frozen Inventory Index.

---

## Universal 9-Skill Core

Verified from Stream 2 extraction across the 10 existing isol8 agents — 9/10 have the same 9 skills:

```
gog, slack, fast-io, taskr, summarize, n8n-workflow, capability-evolver, skill-vetter, sona-security-audit
```

Every Atlas-emitted openclaw.json includes this core automatically (per the standard agent shape). Generator merges with user-vetted skill additions deduplicated. Vet flow at S9: user can selectively reject any core skill via the same selective-reject mechanism, with explicit warning ("rejecting all 9 likely produces a non-functional agent that won't integrate with isol8's standard tooling").
