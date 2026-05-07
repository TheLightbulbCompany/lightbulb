# TOOLS.md

Reference for every skill, pipeline, Node script, and contract Atlas relies on. AGENTS.md is the operating philosophy; TOOLS.md is the implementation reference.

---

## Lobster (Pipeline Orchestration)

Eight `.lobster` pipelines. Each maps 1:1 to a phase or operational concern.

| Pipeline | Trigger | Purpose | LLM calls |
|---|---|---|---|
| `intake-and-research.lobster` | webhook `/hooks/atlas-build-start` | Phases 1 + 2: identity lock, tier detect, stack overlap, cookbook + failure-case research, brief synthesis | 1–2 (overlap conditional + research) |
| `architect-proposal.lobster` | from intake completion | Phase 3: skeleton, classifier, skill recommend, archetype synthesis | 1 |
| `vet-walk.lobster` | per-requirement loop | Phase 4: walker iterates matrix, presents each row, persists decision after each gate confirmation | 0 |
| `recap-and-cost.lobster` | from vet completion | Phases 5 + 6: original→final diff + deterministic-vs-LLM cost-impact | 0 |
| `build.lobster` | webhook `/hooks/atlas-build-confirm` (after S9 architecture sign-off) | Phase 7: pre-write scans + S10 file-write agentgate + atomic write | 0 |
| `publish.lobster` | webhook `/hooks/atlas-publish-confirm` (Phase 8 opt-in) | Phase 8: clawhavoc HARD + sensitive-sniff WARN + listing build + S11 + marketplace-submit stub | 0 |
| `cookbook-refresh.lobster` | cron daily 04:00 | Refreshes OpenClaw doc cache from `https://docs.openclaw.ai/llms.txt` index | 0 |
| `citation-verification.lobster` | cron quarterly (architecture addition #3) | Quarterly verification pass on every external citation in Atlas's AGENTS.md and any Atlas-generated agents' AGENTS.md; 2 consecutive 403/404/changed-content → stale-citation warning | 0 |

---

## llm-task

Atlas uses 1–3 llm-task calls per build (typical 2.4 calls/build). Per-call provider+model override is the documented OpenClaw pattern (verified at `https://docs.openclaw.ai/tools/llm-task.md` — `provider` and `model` are accepted optional parameters with default-override semantics; not paraphrased tighter than the docs say).

### Tier-Aware Routing

Every llm-task call in Atlas's pipelines reads `$intake.json.tier` and emits `provider` + `model` per the routing table. Atlas's `openclaw.json` `llm-task.config` defaults are dev/test fallbacks only — production calls MUST include explicit overrides.

| Tier | provider | model (Atlas's own LLM use) | Recommended for built agents — cheap path | Recommended for built agents — premium path |
|---|---|---|---|---|
| `byo_openai` | `openai` | `gpt-5-mini` | `gpt-5-mini` | `gpt-5` |
| `byo_claude` | `anthropic` | `claude-haiku-4-5` | `claude-haiku-4-5` | `claude-sonnet-4-6` (premium-default), `claude-opus-4-7` (only when reasoning depth genuinely requires it) |
| `hosted_claude` | `anthropic` (via isol8 proxy, 1.5x markup) | `claude-haiku-4-5` | `claude-haiku-4-5` | `claude-sonnet-4-6` default, `claude-opus-4-7` when justified |

Defaults encoded as constants in `atlas-tier-detector.js`. User can override at intake; Atlas warns when override would push projected cost >10x cheap-tier baseline (threshold tagged "v1 default, revisit after 5 user builds" per M2 calibration pattern). Warning shows actual dollar delta at user's projected traffic, not just a multiplier — users intuit money, not multipliers (per L3).

### Tier source — read-both pattern (L1)

`atlas-tier-detector.js` reads tier from `fast-io: isol8/user/tier` (preferred) with env-var fallback `ISOL8_USER_TIER`. Logs which source resolved. If neither resolves, hard-fails build with link to the isol8 settings page where tier provisioning lives (URL set per L5 — verify against current isol8 frontend before deploying).

### Thinking levels

`off` for classification/extraction. `low` for synthesis (research brief, archetype synthesis, overlap-disambig fallback). Never `medium` or `high` in automated pipelines.

### archetype-synthesis shared schema versioning (R5)

The single `archetype-synthesis` LLM call produces both R1 user-story phrasings AND R5 SOUL.md voice/tone guidance via a multi-field output schema. The schema is versioned (`atlas-config/archetype-synthesis-schema-version`) — change-one-break-other risk: a future contributor who modifies voice fields without updating user-story field extraction breaks both downstream populators silently. The version field exists to detect drift; bump the version on any field change and assert during code review that all consumers handle the new version.

---

## fast-io (Persistent Storage)

```
atlas-config/
  identity-locks/{{build_id}}             # immutable identity records {build_id, name, role, scope, locked_at, sha256}
  user-tier                                # cached tier detection {tier, default_model, markup_multiplier, source}
  cookbook-refresh-state                   # last-refreshed timestamps per cookbook source
  archetype-synthesis-schema-version       # current version of the shared archetype-synthesis output schema (R5)

atlas-cookbook/
  openclaw-docs/{{path}}                  # cached OpenClaw doc pages (refreshed daily via cookbook-refresh.lobster)
  agent-snapshots/{{agent_name}}          # snapshots of prior builds at intake time
  failure-cases.json                      # curated corpus, refreshed Mon + Thu (architecture addition #2)
  user-resolutions/{{topic}}              # user answers to D3 ask-flow gaps; checked first on next query of same topic (same-day dedup before capability-evolver weekly loop)

atlas-builds/
  {{build_id}}/intake                     # locked identity + scope from Phase 1
  {{build_id}}/research-brief             # Phase 2 output (required for Phase 3)
  {{build_id}}/skeleton                   # Phase 3 deterministic scaffold
  {{build_id}}/requirement-matrix         # Phase 3 immutable snapshot — write once
  {{build_id}}/requirement-matrix.sha256  # SHA256 of requirement-matrix at snapshot time (W5 hash-verification pattern)
  {{build_id}}/decisions/{{req_id}}       # per-requirement vet decision (architecture addition #1, drop-resilient resume)
  {{build_id}}/recap                      # Phase 5 diff
  {{build_id}}/cost-estimate              # Phase 6 output
  {{build_id}}/artifacts/{{filename}}     # Phase 3-7 staged file content (drafts) — drafts ≠ disk files (top-level invariant)
  {{build_id}}/cookbook-gaps/{{topic}}    # D3 ask-flow gap log (separate from user-resolutions for analytics)

atlas-pending-submits/{{timestamp}}        # P6 marketplace submit stubs (real-API ship triggers one-time migration)

audit/{{date}}/{{action}}                  # standard audit pattern, 12-month retention
```

---

## Skills

### taskr (ClawHub community skill)

Invocation surface verified from real usage in `vera/intake-resolve.lobster`, `vera/escalation.lobster`, `ora/scheduling-request.lobster`:

```
openclaw.invoke --tool taskr --action {create|read|update} --args-json '{
  "key": "namespaced-string/{{id}}",
  "data": { ... arbitrary payload ... }
}'
```

For Atlas's vet flow specifically: per-requirement taskr entries created at vet start with `status: pending`, updated to `status: approved|changed|dropped` as walker advances. Complements the fast-io decisions log by providing the "what's outstanding right now" view.

OpenClaw docs do not document taskr; ground truth is the verified usage above.

### agentgate (ClawHub community skill)

Installation reference: Ember's BOOTSTRAP.md. skill-vetter pre-install required (per S8). Used at four points in Atlas:

1. Architecture sign-off (S9, end of Phase 6) — sync hard block before Phase 7 file generation
2. Final file write (S10, start of Phase 7 after pre-write scans pass) — sync hard block, presents file list + bundle SHA256
3. Marketplace submit (S11, end of Phase 8) — sync hard block, presents listing + bundle SHA256 for re-confirmation against S10 hash
4. Security exception requests during build (A14) — sync hard block when user requests denied skill with verified-publisher manifest URL

OpenClaw docs do not document agentgate; ground truth is the verified usage above.

### Other skills (brief)

- `fast-io` — persistent K/V storage; key schema above
- `summarize` — no-LLM compression CLI; used in Phase 2 to compress cookbook reads into tractable research-brief input
- `n8n-workflow` — webhook glue for `/hooks/atlas-*` endpoints
- `capability-evolver` — weekly cron (Sun 06:00) reads `atlas-builds/*/decisions/*` and emits learning summary; tunes v1-default thresholds
- `skill-vetter` — runs at bootstrap on every installed skill; runs again at build-time inside `atlas-skill-recommender.js` before any skill is recommended; refusal includes vetter's structured findings (`{check, severity, reason, remediation}` per finding) per S2 audit-log granularity
- `sona-security-audit` — runtime monitoring of installed skills; daily background; surfaces alerts via Slack; Atlas does nothing on alert beyond surfacing (monitoring infrastructure, not action infrastructure per S3)
- `gog` — Google Workspace; used for any user-supplied design docs; optional
- `slack` — primary user interface for vet flow + agentgate confirmations

---

## Deterministic Scripts (local, stdin-JSON → stdout-JSON)

Every Node script follows the `stdin-JSON → stdout-JSON` convention (orchestration-extension contract per B2 — future orchestrators can hook between phases without modifying internals).

| Script | Purpose |
|---|---|
| `atlas-identity-lock.js` | Locks new agent's name/role/scope at intake; emits immutable identity record with SHA256 verification on later reads |
| `atlas-tier-detector.js` | Reads user's isol8 LLM tier via fast-io with env-var fallback; logs which source resolved; hard-fails on neither |
| `atlas-stack-overlap.js` | Reads `~/.openclaw/agents/*/openclaw.json` structured fields only (never AGENTS.md prose); returns `{overlap_score: 0-100, conflicting_agents, recommendation: "extend"|"separate"|"no_overlap"|"ambiguous"}`; routes to llm-task fallback per B5 if structured fields are absent (never silent prose-parsing fallback per A6) |
| `atlas-cookbook-reader.js` | Reads from two-source cookbook (OpenClaw docs cache + agent snapshots); applies frozen source-preference table (D2, see Cookbook Source Preference below) |
| `atlas-failure-case-router.js` | Filters failure-case corpus by archetype tag; refuses to inject any case whose source URL isn't marked `verified` in the corpus entry per H4 |
| `atlas-skeleton-generator.js` | Emits 6 .md scaffolds + .lobster pipeline stubs from analog template + vetted requirements; enforces R1 (`{{outcome}}` required, render fails if absent), R3 (contract-violation comment in template), R4 (Phase 7 hard fail on empty boundary section + exact heading match for "What [Agent] Never Does — Non-Negotiable") |
| `atlas-det-llm-classifier.js` | Per-requirement classification deterministic / LLM / hybrid with rationale; flags warnings when generated agent's projected LLM ratio exceeds 15% (M2 threshold tagged "v1 default, revisit after 5 user builds") |
| `atlas-skill-recommender.js` | Given archetype + requirements, emits recommended skill list with reasoning + deny list; runs skill-vetter as sub-process per S2 before any skill is added; refuses any C1–C8 ClawHavoc skill universally per C9 (no source-context exception heuristic per C10) |
| `atlas-requirement-walker.js` | Drives Step 4 vet loop; persists per-requirement state in fast-io after every gate confirmation (architecture addition #1, drop-resilient resume from next un-vetted row); refuses to advance past unresolved row; signal-detection for H6 anti-overcomplication via fuzzy match (Node-side, ≥1 strong-signal token AND <30 words OR ≥2 strong-signal tokens at any length) |
| `atlas-recap-diff.js` | Phase 5: reads original requirement matrix (via SHA256 hash verification per W5; aborts on hash mismatch) and final decisions; emits diff table |
| `atlas-cost-counter.js` | Counts deterministic vs LLM nodes per pipeline; emits architectural-replacement count tier-agnostic per L7; outputs labeled "projected" never "tested" per H5 |
| `atlas-tier-cost-estimator.js` | Applies tier rate card to LLM call counts; per L4 markup is line-itemed not buried; per L8 what-if comparison visually separated in output card (different section, "What-if: not your active tier") |
| `atlas-clawhavoc-enforcer.js` | Scans bundle (openclaw.json + workspace .md + .lobster + .js) for any reference to `CLAWHAVOC_DENYLIST` (frozen exported constant via `Object.freeze()` at module load per P2); used at build-time (W7) and publish-time (W8); see Asymmetric Override Semantics below |
| `atlas-sensitive-sniff.js` | Regex + entropy scan for likely API keys, OAuth tokens, env-var values (not names), hardcoded user identifiers in BOOTSTRAP.md/USER.md; returns warnings (not blocks) with line-level pointers; user override audit-logged with structured `{type, line, matched_pattern}` per dismissed warning per P3 |
| `atlas-listing-builder.js` | Phase 8: zero-LLM deterministic extraction from already-vetted artifacts (P4 architectural property — can't drift from vetted state, cross-referenced from A10); emits JSON listing card with `name, archetype, requirements_covered_count, requirements_partial_count, requirements_uncovered_count, vet_completion_status, security_stack[], model_tier_requirements, agentgate_gates[], never_does[]` |
| `atlas-marketplace-submit.js` | Phase 8 stubbed per F2: writes payload to `fast-io: atlas-pending-submits/{{timestamp}}` + local preview file `~/.openclaw/atlas/preview-submits/{{timestamp}}.json`; TODO comment lists what real API needs (auth, bundle upload, listing metadata schema, status callback); one-time migration on real-API ship surfaces accumulated stubs to user |
| `atlas-activation-check.js` | Bootstrap validator (env vars, fast-io defaults, cron, channels, security stack, ClawHavoc denylist completeness) |
| `atlas-citation-verifier.js` | Architecture addition #3: quarterly cron pass on every external citation in Atlas's AGENTS.md and any Atlas-generated agents' AGENTS.md; on 2 consecutive 403/404/changed-content responses, surfaces stale-citation warning via Slack |

---

## Click-to-Connect Integrations

Atlas does not interrogate the user about integrations during onboarding. Click-to-connect skills are declared `enabled: false` in openclaw.json with explanatory `note`; Atlas only references them during the Phase 1 stack-overlap check.

When a user requests a build that needs a skill they haven't connected:

> "To do <X>, connect <service> in your isol8 settings. Supported: <option1>, <option2>, <option3>. You can connect more than one."

Never asked during onboarding — only when needed.

---

## Cost Output Formatting

Phase 6 cost-impact card structure (enforced by `atlas-tier-cost-estimator.js` output schema):

1. **Deterministic-replacement count first** (tier-agnostic per L7, top of card)
2. **Per-tier $ delta vs all-LLM baseline** at user's active tier only per L8, with markup line-itemed per L4
3. **What-if comparison** (only if requested) — visually separated section, header reads "What-if: not your active tier", clearly distinguished so users don't screenshot the wrong number when sharing with teammates

Footer line: *"Deterministic-replacement ratio is comparable within an archetype, not across archetypes. Atlas's 97.8% reflects its meta-agent archetype where most work is structured. Cross-archetype comparisons (e.g., Pitch's 91% vs Atlas's 97.8%) are not apples-to-apples."*

Every dollar figure labeled "projected, not tested" per H5. The architectural ratio is the precise signal; dollar projections use a 5K-token-average baseline (could be 2x high or 2x low — averaging walker iterations ~500 tokens with file-generation calls ~10K+ at a midpoint).

---

## Phase Output Contract (Orchestration Extension)

Per B2: the orchestration-extension contract is that future orchestrators consume Atlas's phase output keys (`atlas-builds/{{build_id}}/intake`, `.../research-brief`, `.../skeleton`, `.../requirement-matrix`, `.../decisions/`, `.../recap`, `.../cost-estimate`, `.../artifacts/`), they do not reach into Atlas's internals. Phase boundaries are file-system observable; each phase writes its output to a distinct fast-io key.

A future orchestrator (e.g., Paperclip in v2) hooks in by reading these keys and dispatching downstream actions, NOT by modifying Atlas's pipelines or scripts.

---

## Stack Overlap (B4)

`atlas-stack-overlap.js` rules:

- **Read structured fields only.** Parse `skills[]` from openclaw.json, structured `description` field if present, `archetype` tag (added to schema for Atlas-generated agents). Never parse AGENTS.md prose.
- **Recommendation enum has 4 values:** `extend` (overlap >70% AND archetype keyword match), `separate` (some capability overlap but distinct enough), `no_overlap` (zero detected overlap, distinct from `separate` for cleaner UX + audit log), `ambiguous` (overlap_score in 30–70 middle band).
- **On `ambiguous`, route to llm-task fallback per B5** with per-tier provider+model override (consistent with L2's pattern); LLM emits `{recommendation, confidence, reasoning}` so the user sees the LLM's reasoning whenever one was needed.
- **On absent structured fields, return "insufficient structured data" and route to llm-task fallback per A6.** Never silent regress to prose parsing.

Thresholds (70%, 30%) tagged "v1 default, revisit after 5 user builds" per the calibration pattern.

---

## Anti-Overcomplication Signal Detection (H6)

`atlas-requirement-walker.js` fuzzy-matches user input against a strong-signal token list:

```
const STRONG_SIGNAL_TOKENS = ["simple", "cleanest", "overthink", "enough options", "one shot", "skip", "stop"];
```

Trigger logic:
- ≥1 strong-signal token AND message <30 words → fires
- ≥2 strong-signal tokens at any length → fires

When triggered, walker switches to collapse-to-cleanest-path mode for the rest of the section: defaults to most common pattern from analog read, surfaces only the choice that actually changes outcomes.

Both the token list and trigger thresholds tagged "v1 default, revisit after 5 user builds." Capability-evolver may extend the token list based on user-corrections in `atlas-builds/*/decisions/`.

---

## Atomic Write Contract (W7)

`atlas-file-generator.js` follows staging-dir-then-mv pattern. Filesystem-level atomicity at the `mv` step.

```
1. All file content already in fast-io at atlas-builds/{{build_id}}/artifacts/{{filename}} (staged during Phases 3-7)
2. Disk write step:
   a. mkdir ~/.openclaw/agents/.atlas-staging-{{build_id}}/
   b. Write all artifact files to staging dir
   c. Verify file count + per-file SHA256 against staged artifact hash in fast-io
   d. On success: mv ~/.openclaw/agents/.atlas-staging-{{build_id}}/ ~/.openclaw/agents/{{agent_name}}/  (atomic on POSIX/local FS)
   e. On any failure: rm -rf ~/.openclaw/agents/.atlas-staging-{{build_id}}/ + structured error report listing which file failed AND including the staged artifact hashes from fast-io (so debugging doesn't require re-deriving expected content)
```

No two-phase commit, no fast-io transaction semantics — just staging-dir-and-mv at the filesystem layer. Documented explicitly so future contributors don't half-implement: half-atomicity is worse than no atomicity.

---

## Hash Verification (W5)

Pattern, not fast-io immutability guarantee.

`atlas-skeleton-generator.js` writes the original requirement matrix to `atlas-builds/{{build_id}}/requirement-matrix` AND its SHA256 to `atlas-builds/{{build_id}}/requirement-matrix.sha256` at snapshot time.

`atlas-recap-diff.js` recomputes the hash on read in Step 5 and aborts the recap with a clear error if it doesn't match.

If we later confirm fast-io has native write-once support, we collapse to that and remove the hash. Until then, this hash-verification pattern is what gives us "immutable enough."

Same pattern applied for all artifacts where immutability is load-bearing: identity locks (`atlas-config/identity-locks/{{build_id}}` + `.sha256`), bundle hashes (S10 → S11 continuity per Marketplace Boundary Contract).

---

## Audit Log Structure

Base entry: `audit/{{date}}/{{action}}` with `{action, build_id, who_authorized, what_changed, when, source_phase}`. 12-month retention.

Elevated audit detail (per requirement vet):

- **Skill-vetter rejections (S2):** structured `{check, severity, reason, remediation}` per finding, not flat text
- **Sensitive-data warning dismissals (P3):** structured `{type, line, matched_pattern}` per dismissed warning
- **Security-exception approvals (A14):** verified-publisher URL + skill-vetter pass timestamp + structured-reason picklist (`legacy_data_migration / vendor_requirement / one_time_emergency / other_with_explanation`); when `other_with_explanation` is selected, free-text justification has 50-char minimum
- **ClawHavoc refusals (C10):** structured enum for user's subsequent action (`provided_verified_url / abandoned_request / requested_human_help / other`)
- **Cookbook gaps (D3):** gap entry to `atlas-builds/{{build_id}}/cookbook-gaps/{{topic}}` for analytics; user resolution to separate key `atlas-cookbook/user-resolutions/{{topic}}` for same-day dedup before capability-evolver weekly loop

---

## Security

### Asymmetric Override Semantics (C1–C8 rationale)

The build-time vs publish-time override asymmetry is intentional:

- **Build-time:** security-exception agentgate (A14) allows override after explicit user confirmation + verified-publisher URL + skill-vetter pass. Rationale: the user is configuring an agent in their own environment; if they have legitimate reason to install a denied skill (legacy data migration, vendor requirement) and can produce verified provenance, that's a knowing, audit-logged choice in a non-public-exposure context.
- **Publish-time:** no override path. Marketplace submission is a public-exposure event with different risk class — buyers downloading the agent inherit any denied-skill dependency without the same agent-builder context.

Future contributors: do NOT "fix" this inconsistency. The asymmetry is the policy.

### Marketplace Boundary Contract (C11)

Three no-override hard refuses at the marketplace boundary, treated as a single named policy:

1. **ClawHavoc denylist on publish (P1):** `atlas-clawhavoc-enforcer.js` HARD BLOCK with no override path
2. **Bundle continuity divergence (S11):** any divergence between S10 file-write SHA256 and S11 marketplace-submit SHA256 → HARD REFUSE (publish.lobster aborts, not warning)
3. **ClawHavoc skill in published bundle (C11):** same enforcer as P1, same no-override posture

Three places where public-exposure events are hard-refused with no override. One policy. Documented as the Marketplace Boundary Contract so it reads as one thing not three coincidences.

### Verified Source Definition (C10)

Required reading for any user requesting installation of a ClawHavoc-listed skill (refusal message links here).

**A verified source is:**
- A publisher-signed manifest URL from the official source domain (e.g., the publisher's `*.com/skills/manifest/...` path with valid signature)
- A signed release artifact from the publisher's GitHub releases page with verifiable PGP signature
- A direct URL to the skill's manifest in the publisher's official documentation

**A verified source is NOT:**
- A ClawHub community comment recommending the skill
- A third-party repost or fork of the skill manifest
- A GitHub fork of unknown provenance
- A direct link to a skill on ClawHub's `*/community-skills/` path (community-published, not publisher-signed)
- A search-result link

**To obtain a verified URL:**
- Check the publisher's official documentation
- Look for signed releases on the publisher's GitHub
- Contact the publisher directly via verified channels

When a user provides a URL claiming verification, `atlas-skill-recommender.js` runs `skill-vetter` against it as a sub-process before reconsidering. Only on vetter pass does the skill become recommendable, with audit log entry recording the verified-source URL and the vetter's pass record.

### ClawHavoc Denylist Provenance (P1)

The canonical denylist (`salesforce-api`, `pipedrive-api`, `attio-api`, `zoho-recruit`, `zoho-crm`, `gmail`, `mailchimp`, `fathom-api`) is isol8's curated list, maintained internally based on public security research. Lives as a frozen `Object.freeze`d exported constant (`CLAWHAVOC_DENYLIST`) at the top of `atlas-clawhavoc-enforcer.js` per P2 — single-file update if the list ever changes.

No specific publication cited inline. During Atlas's bootstrap vet, a Trend Micro Feb 2026 attribution was proposed as inline citation in refusal messages; verification attempt returned HTTP 403 (could be bot blocking, could be non-existent page, could be auth gating); per H4 the citation could not ship. See AGENTS.md "Build Session Learnings" appendix for the worked example.

If isol8 later publishes a public docs page with the denylist provenance, the refusal-message text and this section update to link there. Until then, refusal text is generic: *"This skill is on isol8's ClawHavoc avoidance list — install from official source via skill-vetter, then re-attempt publish."*

---

## Cookbook Source Preference Table (D2)

Frozen exported constant in `atlas-cookbook-reader.js` (same `Object.freeze()` pattern as `CLAWHAVOC_DENYLIST` per P2):

```
const COOKBOOK_SOURCE_PREFERENCE = Object.freeze({
  "skill_manifest_schemas":         "agent_snapshots",  // OpenClaw plugin manifest doc doesn't cover skill manifests
  "lobster_branching_error_syntax": "agent_snapshots",  // OpenClaw lobster doc is shallow
  "openclaw_json_trust_boundaries": "agent_snapshots",  // No consolidated docs page
  "bootstrap_cron_webhook_patterns":"agent_snapshots",  // Existing BOOTSTRAP.md files have the patterns
  "lobster_basic_schema":           "openclaw_docs",    // tools/lobster.md covers top-level schema
  "llm_task_call_shape":            "openclaw_docs",    // tools/llm-task.md is authoritative
  "openclaw_json_top_level":        "openclaw_docs",    // gateway/configuration.md is authoritative
  "channels":                       "openclaw_docs",    // channels/* pages are comprehensive
  "clawhub_community_skills":       "agent_snapshots",  // taskr, agentgate, etc. — verified usage in agents
});
```

### Update process (D2 add)

When OpenClaw eventually documents what they currently don't, the source-preference may need to flip back to docs for some topics. To revisit:

- **Trigger:** OpenClaw doc index (`https://docs.openclaw.ai/llms.txt`) adds a previously-missing topic AND `cookbook-refresh.lobster` detects the new page.
- **Decision:** human review of the new doc page; if it's authoritative and complete for the topic, flip the preference to `openclaw_docs`. If partial, keep `agent_snapshots` and add a note to the table.
- **Frequency:** capability-evolver weekly cron flags any cookbook-source query patterns that suggest stale preference; quarterly human review confirms or updates.

Without this update process the table calcifies and we'll still be preferring agent snapshots three years from now when the docs caught up.

---

## Marketplace Submit Stub (P6)

`atlas-marketplace-submit.js` is the F2 stub. Behavior:

1. Writes payload to `fast-io: atlas-pending-submits/{{timestamp}}` (auditable, queryable)
2. Writes local preview file at `~/.openclaw/atlas/preview-submits/{{timestamp}}.json` for user inspection
3. Surfaces to user: *"Marketplace submit queued at {{timestamp}}. Real API not yet available; preview file at ~/.openclaw/atlas/preview-submits/{{timestamp}}.json shows exactly what would have been submitted."*

### TODO when real API ships

Top-of-file TODO comment lists what the real API needs to support:
- Auth (per-user token vs OAuth?)
- Bundle upload (multipart vs signed URL?)
- Listing metadata schema (does it match `atlas-listing-builder.js` output, or do we transform?)
- Status callback (webhook vs polling?)

### One-time migration on real-API ship

When `atlas-marketplace-submit.js` is replaced with the real implementation, it ALSO runs a one-time migration: scans `atlas-pending-submits/*` for accumulated stubs and surfaces *"You have N pending submits from before the marketplace launched — review and submit?"* to the user. Documented as a launch-time obligation so stubbed submits don't become silent dead weight.

`pending-submits-prompt` cron (weekly Mon 09:00) reminds the user of accumulated stubs in the meantime; archive_pending_submits_after_days from USER.md controls when reminders stop.

---

## PRD Format Enforcement

`atlas-skeleton-generator.js` enforces PRD format on every generated agent's AGENTS.md.

- **R1 user-story format:** template literally reads `"As a {{agent_name}}, I should {{action}} so that {{outcome}}"`. Empty `{{outcome}}` field → render fails. The "so that" clause is required because requirements without purpose ship as mechanical-but-not-purposeful, a frequent failure mode.
- **R3 contract violation comment:** template comment in generated AGENTS.md reads: *"Drift between vetted gate-classification and AGENTS.md autonomy section is a contract violation, not a stylistic choice. Update both together or neither."*
- **R4 hard fail on empty boundary section:** Phase 7 file generation aborts if "What [Agent] Never Does — Non-Negotiable" section is empty. Every agent must have at least one hard boundary; an agent with no never-does is an agent with calibrated-autonomy issues that haven't been thought through.
- **R4 exact heading match:** verbatim heading "What [Agent] Never Does — Non-Negotiable" enforced via exact string check at template level. Drift on this heading is a contract violation that fails Phase 7.
- **R5 SOUL.md voice/tone:** populated from the shared archetype-synthesis output (versioned schema per llm-task section above).

User vets each generated section during Step 4 walk per W4. No PRD ships without vetted phrasing.
