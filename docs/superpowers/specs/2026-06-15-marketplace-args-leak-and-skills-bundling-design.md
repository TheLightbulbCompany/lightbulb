# Marketplace: args-secret leak + skills-bundling correctness — Design

**Date:** 2026-06-15
**Status:** Design (awaiting review → implementation plan)
**Area:** backend marketplace publish/export (`apps/backend/core/services/`)
**Branch:** `worktree-marketplace-args-leak-skills-bundling`

---

## 0. Reading note — this supersedes the earlier "effective-config" draft

An earlier draft framed a "Problem 2: published bundle ≠ what the agent runs"
and proposed publishing from each agent's **per-workspace** `openclaw.json`.
**That premise was wrong** and is discarded here. Live verification (see §2 +
§11) proved there is **no per-agent MCP scoping**: every agent runs the single
**global** `mcp.servers`, and the per-workspace `openclaw.json` files are inert
deploy artifacts that OpenClaw never loads. The export already reads the global
config, so it already publishes what agents actually run. The real defects are
narrower.

---

## 1. Problems (verified live, prod, 2026-06-15)

Founders' org `org_3DBS8G83oPrMm49yN2b70EjoFpN` — the only meaningful tenant
(pre-GA, no external hirers). Pre-onboarding hardening, not an active incident.

### Problem 1 — credential leak via MCP `args[]`

The slice scrubber (`catalog_slice._scrub_secret_fields`) and the publish gate
(`marketplace_safety.assert_slice_scrubbed`) key on **secret-shaped field names**
(`apiKey`/`env`/`headers`/`webhookToken`) and `client_secret`. A secret passed
as a **positional CLI arg** — `npx -y instantly-mcp --api-key <KEY>` — is neither,
so it survives both. The global `instantly` server carries the key inline, the
export reads global, and 7 of 9 published listings (echo, jarvis, lens, ora,
pulse, stitch, thread) ship the **real** Instantly key (hash
`sha256:7e883fb40b51 len=68`, identical to the live key). Confirmed in container
logs: `instantly-mcp --api-key YzI4…` (142 start attempts over 9 days).

### Problem 2 — skills-bundling step is not aligned with intended behavior

Intended behavior (owner-confirmed): **all of an agent's own (workspace) skills
ship automatically; global skills may optionally be added by the seller.**

Current behavior:
- Workspace skills **do** auto-ship — `_build_tarball` tars the entire workspace
  (incl. `skills/`); deploy untars it; the buyer's gateway discovers them
  (`<workspace>/skills`, default-on). So the auto-ship requirement is already
  met end-to-end. `selected_skills` only ever affected the slice config, never
  the tarball, so it cannot suppress them.
- The **"add a skill" picker is wired to the wrong source.**
  `enumerate_for_preview` builds `available_skills` from the agent's **global
  config entry** `skills` list (`agent_entry.get("skills")`) — empty for the
  founders' agents — instead of from the container's **global** skills (global
  `skills.entries` + managed `~/.openclaw/skills` + bundled). So the affordance
  for "optionally add a global skill" points at the wrong (empty) list, and the
  preview misrepresents what ships.

(Today this has no functional impact — workspace skills auto-ship already, and
the founders have no global `skills.entries` or managed skills to add — but the
wiring is incorrect and the picker is misleading.)

---

## 2. Verified model (reference — prevents the wrong model resurfacing)

Established by reading the fork (`~/Desktop/agent`, the image we run) + probing
prod EFS/logs/cache. **This section is load-bearing: the earlier draft went
wrong by trusting the per-workspace config files.**

### MCP is global-only — no per-agent scoping

- OpenClaw resolves config **only** from `OPENCLAW_CONFIG_PATH`/stateDir
  (`config/paths.ts`) — no cwd walk, no per-workspace read. The gateway loads the
  **single global** `openclaw.json`; its `mcp.servers` is shared by every agent.
- The per-workspace `workspaces/<id>/openclaw.json` files are **inert** — verbatim
  seller snapshots carried by the deploy (`catalog_service.deploy_from_artifact`
  untars the seller tarball as-is and re-keys only the *global* entry). They are
  never loaded. Proof: servers present **only** in a nested config and never in
  global (`arxiv`, `pubmed`) have **0** start attempts in 9 days, while global
  servers start constantly (instantly 1456, paperclip 1173, …). `perplexity`
  appeared only transiently because a separate CPU-load test added it to the
  **global** config (observed live as a `[reload] mcp.servers.perplexity` hot
  reload), not because Lens's nested config loaded.
- The global `mcp.servers` is edited live (a CPU test cycled
  `stripe/perplexity/apollo/attio/test-*` in and out). Steady state is
  `{instantly, paperclip}`. Any point-in-time read is a snapshot.

### Identity (consistent where it matters)

- The agent **UUID** (Paperclip-assigned) is used consistently by Isol8: global
  `agents.list[].id`, EFS workspace dir name, session routing, DDB. Short names
  (`lens`, `stitch`) appear only inside the inert nested configs; there are **no**
  `agents/<short-name>/` runtime dirs (only `agents/<uuid>/`), so they are
  cosmetic and not load-bearing.

### Skills model

- ~47 **bundled** skills ship in the image and are **enabled by default**
  (`isBundledSkillAllowed` returns true when `skills.allowBundled` is empty).
  They live in the image, never in EFS config; the buyer's identical image has
  them too. The Skill Store's "Installed (47)" is this set.
- Skill load precedence (highest→lowest): `<ws>/skills` → `<ws>/.agents/skills` →
  `~/.agents/skills` → managed `~/.openclaw/skills` (`--global` install target,
  shared) → bundled (image) → `skills.load.extraDirs` + plugin skills.
- `skills.install` default destination is the agent's **workspace** `skills/`;
  `--global` targets the shared managed root.
- For the founders today: managed root and `extraDirs` are **empty**, global
  `skills.entries` is empty. Every in-use skill is bundled (image) or
  workspace-local. Workspace skills are discovered + default-on.

---

## 3. Scope

**In scope**

1. **Fix 1** — args-aware secret scrub + publish-blocking gate.
2. **Fix 2** — skills-bundling correctness:
   - Reflect that workspace skills auto-ship (informational; not selectable).
   - Wire the global-skills add-list to the correct source (global
     `skills.entries` + managed `~/.openclaw/skills`; surface bundled as
     "already on buyer"); package selected global skills into the artifact.
3. Unit tests (TDD) for both.

**Out of scope** (flagged, not built)

- Per-agent "effective config" publishing (the discarded Problem 2 — there is no
  per-agent scoping).
- Buyer key-provisioning UX (keyed servers/skills ship blanked; a buyer cannot
  supply their own key yet — its own project).
- Deploy-time re-keying of inert nested configs (cosmetic).

**Manual remediation runbook** (operator, §9) — rotate the Instantly key; take
down / republish the 7 leaking listings.

**Separate product issue (not this spec, §10)** — agents configured with research
MCP servers in their (inert) nested configs (e.g. Lens → arxiv/pubmed/perplexity)
do **not** actually run them; per-agent MCP is not in effect.

---

## 4. Design — Fix 1: args-aware secret handling

Two layers, mirroring the existing scrub-then-gate architecture.

### 4a. Scrub (`catalog_slice`)

Extend `_scrub_secret_fields` (applied to every `mcp.servers[*]` and
`skills.entries[*]` copied into a slice) to also walk an entry's `args` list and
blank the **value of a secret flag**:

- Secret flags (case-insensitive): `--api-key`, `--apikey`, `--key`, `--token`,
  `--secret`, `--password`.
- Shapes: separated `["--api-key","<VAL>"]` → next element `""`; joined
  `["--api-key=<VAL>"]` → `"--api-key="`. Flag preserved so the buyer sees what
  to provide.

This neutralises the real instantly leak.

### 4b. Gate (`marketplace_safety.assert_slice_scrubbed`)

Add an args-aware check on the (already-scrubbed) slice that BLOCKS publish
(`has_blocking_credential_findings` → `CredentialLeakError`) when:

- **Post-flag residue:** a non-empty value still follows a secret flag → reuse
  `unscrubbed-credential`.
- **Standalone secret (conservative):** a positional arg **not** behind a known
  flag matching a secret shape — provider prefixes (`sk-`, `sk-ant-`, `ghp_`,
  `gho_`, `xoxb-`, `xoxp-`, `AKIA…`/`ASIA…`) or a high-entropy token ≥40 chars
  (mixed case+digits) — **excluding** npm-package shapes
  (`^@?[a-z0-9][a-z0-9._/-]*$`) so `npx -y <package>` never false-blocks. New
  pattern `arg-secret-suspected`, added to `_BLOCKING_CREDENTIAL_PATTERNS`.

Block-not-scrub on standalone: blanking an arbitrary positional is unsafe; a
human resolves it. Snippets stay redacted (`_redact`).

---

## 5. Design — Fix 2: skills-bundling correctness

### 5a. Workspace skills auto-ship (reflect, don't change)

No behavior change — they already ship via the tarball and work via buyer
discovery. Update `enumerate_for_preview` to return them as an **informational
"ships automatically" list**, distinct from the selectable add-list, so the
wizard stops implying they're opt-in. The tarball path is unchanged.

### 5b. Global-skills add-list (fix the source)

Replace the `available_skills` source. Instead of the agent's global-entry
`skills` list, enumerate the container's **global** skills the buyer would not
otherwise have:

- global `skills.entries` keys (config-registered, may carry secrets), and
- managed `~/.openclaw/skills` skill names (shared, seller-installed).

Surface **bundled** skills separately as informational ("already on the buyer's
image — no action needed"), since adding them is a buyer-side no-op. Today both
real add-sources are empty for the founders, so the add-list is correctly empty
(vs. the current incorrectly-empty list driven by the wrong source).

### 5c. Package selected global skills

For each global skill the seller adds:

- **`skills.entries` (config-registered):** carry the scrubbed entry into the
  slice (existing `extract_agent_slice` ENTRIES path + the §4a args scrub). If the
  entry's source lives in the managed root, also ship its source (next bullet).
- **Managed `~/.openclaw/skills/<name>` (source-based):** copy the skill's source
  into the artifact (alongside the workspace tarball, or as a dedicated
  skills tar), and register it on deploy so the buyer's container has it. This is
  the heavier sub-piece; it is **phaseable** (the wiring + entries path land
  first; managed-source packaging can follow, since no managed skills exist to
  package today). The spec includes it so "global skills can be added" is
  genuinely supported, not just displayed.

---

## 6. Components touched

| File | Change |
|---|---|
| `core/services/catalog_slice.py` | `_scrub_secret_fields`: blank secret-flag arg values. |
| `core/services/marketplace_safety.py` | `assert_slice_scrubbed`: args-aware detection (post-flag residue + conservative standalone); `arg-secret-suspected` in `_BLOCKING_CREDENTIAL_PATTERNS`; secret-shape + npm-package-exclusion helpers. |
| `core/services/agent_export.py` | `enumerate_for_preview`: workspace skills as auto-ship (informational); add-list from global `skills.entries` + managed root (not the global agent-entry `skills`); bundled surfaced informationally. Package selected global skills (entries via slice; managed source via artifact — phaseable). |
| `schemas/` (artifact preview response) | Add fields distinguishing auto-ship vs. addable skills if the response shape needs it. |

No per-agent config reads, no infra, no deploy-path changes.

---

## 7. Error handling / edge cases

- Args scrub must not touch non-secret flags (`-y`, `--transport`, package
  specs); only the enumerated secret flags trigger blanking.
- Standalone detector must exclude npm-package shapes (tested) to avoid
  false-blocking `npx -y <pkg>`.
- All existing leak-gate behavior (nested secret-named fields, `client_secret`)
  preserved — args handling is additive.
- Missing/empty global `skills.entries` / managed root → add-list empty (correct,
  not an error).
- Managed-skill source packaging must reject symlinks / credential-shaped files
  (reuse the tarball filter posture in `agent_export`).

---

## 8. Testing plan (TDD)

Targeted unit tests during development; full suite at final verification (no full
`pnpm test`; targeted backend pytest).

`tests/unit/services/test_catalog_slice.py`
- `--api-key <VAL>` → `""`; `--api-key=<VAL>` → `--api-key=`; `-y <package>`
  untouched; existing `env`/`apiKey` scrub unchanged.

`tests/unit/services/test_marketplace_safety.py`
- inline-keyed server (scrub bypassed) → BLOCK; provider-prefixed standalone →
  BLOCK; ≥40-char high-entropy non-package → BLOCK; `npx -y <pkg>` → not flagged;
  properly-scrubbed slice → no findings.

`tests/unit/services/test_agent_export.py`
- `enumerate_for_preview`: workspace skills returned as auto-ship list; add-list
  sourced from global `skills.entries` + managed root (fixtures), not the global
  agent-entry `skills`; bundled surfaced informationally; empty global → empty
  add-list.
- Selected global entry → scrubbed entry in slice; (phase 2) managed skill source
  packaged into artifact.

---

## 9. Manual remediation runbook (operator)

After Fix 1 merge + deploy:

1. **Rotate** the Instantly API key (Instantly dashboard). Leaked key == live key,
   so one rotation invalidates all 7 copies. Old immutable S3 artifacts keep the
   dead key (acceptable post-rotation).
2. For the 7 leaking listings (echo, jarvis, lens, ora, pulse, stitch, thread):
   **take down** unwanted ones (`/listings/{id}/takedown`), or **republish** the
   keepers via the wizard (`/republish` → artifact-from-agent → submit →
   approve). Re-export now scrubs the arg key.

---

## 10. Known gaps / separate findings

- **Per-agent MCP not in effect** — agents whose nested configs declare extra
  servers (Lens → arxiv/pubmed/perplexity) never run them (MCP is global-only).
  If per-agent capability is desired, that is a separate config/runtime
  investigation (and would be a prerequisite before the marketplace ever needs
  per-agent capture). Product issue, not marketplace.
- **Buyer key-provisioning UX** — keyed servers/skills ship blanked; buyers can't
  supply their own key yet.
- **Managed-skill source packaging (5c)** — phaseable; nothing to package today.

---

## 11. Appendix — evidence (2026-06-15, prod)

- Leak: 7/9 published `openclaw-slice.json` carry `instantly --api-key
  sha256:7e883fb40b51 len=68`; identical to the live global instantly key.
- No per-agent MCP: config resolution is stateDir-global-only (`config/paths.ts`);
  nested-only `arxiv`/`pubmed` = **0** bundle-mcp start attempts (9d) vs. global
  `instantly` = 142; live `[reload]` event added `mcp.servers.perplexity` to the
  **global** config (a CPU-load test), explaining the only transient perplexity
  activity.
- MCP servers actually started (9d): attio 1680, perplexity 1653, apollo 1652,
  stripe 1623, instantly 1456, posthog 1413, paperclip 1173, fast-io 673,
  twilio 51, test-* 2 — all global-config servers.
- Identity: 11 `agents/<uuid>/` dirs + `main`; zero `agents/<short-name>/` dirs.
- Skills: global `skills` block = `{"install":{"nodeManager":"npm"}}` (no entries,
  no allowBundled, no load.extraDirs); managed root `~/.openclaw/skills` empty;
  Lens workspace `skills/` has 10 dirs (auto-ship via tarball); bundled enabled by
  default when `allowBundled` empty.
