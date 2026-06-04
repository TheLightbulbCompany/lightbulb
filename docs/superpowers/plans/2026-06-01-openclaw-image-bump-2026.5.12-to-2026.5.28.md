# OpenClaw Image Bump: 2026.5.12 → latest (2026.5.28 stable) — Migration Plan

**Date:** 2026-06-01 · **For:** the engineer doing the bump
**Method:** dynamic workflow — 11 surface analysts → adversarial verify → completeness critic → synthesis (24 agents).
**Version delta:** pin `2026.5.12` → upstream HEAD ≈ `2026.5.28` (689e8ec). 6369 commits in range. Latest published = `v2026.6.1-beta.2` (prerelease, beyond the local checkout).

---

## 1. Verdict & risk

**This is a multi-PR migration, not a clean tag bump.** There is **one true boot-bricking blocker** that must land in the *same change* as the version bump, the rollout touches **three hand-mirrored literals** that drift silently, and the **existing-container path needs an on-disk config backfill** that has no owner yet.

**Two "scary global signals" turned out NOT to be blockers (red herrings):**
- **Protocol floor did NOT move.** `src/gateway/protocol/version.ts` was *relocated* to `packages/gateway-protocol/src/version.ts` (commit `b1117d9862`, #87797), still `PROTOCOL_VERSION=4 / MIN_CLIENT=4 / MIN_PROBE=4`. Our v4 handshake passes. (A `4→5→4` toggle happened mid-range but was reverted by `ad155fbbd7` on 2026-05-17; no published release carries v5.)
- **zod→TypeBox migration (#86639)** touched the *plugin / JSON-schema* validators, **not** the zod `OpenClawSchema` that gates `openclaw.json` (still zod, still `.strict()`).

**Target decision (master switch):** pin **`2026.5.28`** (latest stable bare tag). The newest published image `2026.6.1-beta.2` is a prerelease and sits beyond the analysis basis — **every report verified against 2026.5.28**. If you choose the beta, the protocol/config/auth/plugin floors MUST be re-run against that exact tag first (the unread `2026.5.29..2026.6.1` window is ~3 days; the floor toggled within the verified range, so a re-bump is not impossible).

**Confirmed-real BLOCKER (ranked, the only one):**
1. **`agents.defaults.embeddedHarness: {}` is rejected by the strict `AgentDefaultsSchema` at HEAD → gateway crash-loops at boot.** Removed by `bb46b79d3c` (#85341 "internalize OpenClaw agent runtime"). We emit it at `apps/backend/core/containers/config.py:633`. Fatal on **both** paths: new provisions write it fresh; Track-2 rolls leave the stale key on the existing container's EFS `openclaw.json`. No boot-time `doctor --fix`; `.bak` only restores the same bad file. Verified by 3 reports + re-confirmed against the live file.

---

## 2. What breaks, by severity

Confirmed-real only (verify-rejected items dropped: the supposed `params:{paperclip}` agents.update failure — backend strips it; `sessions.clear` and `model:null` — pre-existing, not bump regressions).

| Sev | Surface | What breaks | Our file:line | Upstream evidence | Fix |
|-----|---------|-------------|---------------|-------------------|-----|
| **BLOCKER** | Provision config | `agents.defaults.embeddedHarness:{}` rejected by strict `AgentDefaultsSchema` → `safeParse` fails → `assertValidGatewayStartupConfigSnapshot` throws → ECS crash-loop. New-provision **and** Track-2 roll. | `config.py:633` | `bb46b79d3c` (#85341): `-embeddedHarness`; `AgentDefaultsSchema` `.strict()` | Delete line 633 (+ stale comment 630-632). **Do not** add `embeddedAgent`. **Plus** backfill-strip the key from every existing container's on-disk config before Track-2 (see §4d). |
| HIGH | Bump mechanics | Dockerfile `FROM` tag is hand-mirrored from `openclaw-version.json#upstream`. JSON-only edit ships a stale base layer with a *lying* version label. | `apps/infra/openclaw/Dockerfile:81` | self-drift; prior incident PR #418 | Edit `Dockerfile:81` in the **same PR** as the `upstream` bump. Add CI assert (§7). |
| HIGH | Plugins / channels | `@openclaw/slack` / `@openclaw/discord` npm pins hand-mirrored to host version. Stale `@2026.5.12` vs `5.28` host risks runtime load drift; bundled-origin **bypasses** the #87477 compat gate → fails silently at runtime, not discovery. | `Dockerfile:216` | HEAD `extensions/slack` + `discord/package.json` both `2026.5.28` | Bump the pin to the new tag in the same PR. Confirm published on npm. |
| HIGH | Bump mechanics | Per-env `dev.tag`/`prod.tag` bumped **before** the extended image is pushed → CDK task def points at a non-existent ECR tag → `ImagePull` fail. | `container-stack.ts:441-443` | two independent workflows (build pushes; deploy consumes) | Two-PR discipline (§4). `aws ecr describe-images --image-ids imageTag=<tag>` before PR-2. |
| HIGH | Desktop | Bundled `openclaw-host` pinned `2026.5.12` speaks exec-approval/browser.proxy/file.* directly. A DMG lagging the container bump drifts on those shapes. | `vendor-sidecars.sh:28` | exec-approval churn `34c441c746` | Bump `OPENCLAW_VERSION`, re-vendor, ship DMG with the container bump. |
| MED | Desktop | Bundled Node `22.16.0` below upstream floor `>=22.19.0`. `engines` is advisory; runtime crash possible on a 22.17–22.19 feature. | `vendor-sidecars.sh:27` | HEAD `package.json engines.node">=22.19.0"`; `e453a39d6b` | Bump `NODE_VERSION="22.19.0"`. N-API/prebuilt → no ABI break. |
| MED | Marketplace | `deploy_from_artifact` copies a seller `agents.list[]` entry through a **denylist** strip (`{model,channels,workspace,id,agentDir}`). Upstream-removed keys (`embeddedHarness`/`agentRuntime`) survive → brick the **buyer's** strict `AgentEntrySchema` on deploy. | `catalog_slice.py:21` (`_STRIPPED_KEYS`), `catalog_service.py:~495` | `AgentEntrySchema` dropped `embeddedHarness` at HEAD, `.strict()` | Add `embeddedHarness`/`embeddedAgent` to a deploy-time strip in `apply_deploy_aux_mutations`, or convert to an allowlist. Bundle with the blocker fix. |
| LOW | Inference (BYOK-anthropic) | `anthropic/claude-opus-4-6-v1` carries a Bedrock-only `-v1` suffix the direct anthropic catalog rejects → `UnknownModelError`. **Pre-existing** (broken today), not a bump regression. | `config.py:332` | HEAD `extensions/anthropic/openclaw.plugin.json` ids bare; `-v1` only in `amazon-bedrock/discovery.ts:51` | Primary → `anthropic/claude-opus-4-6` (drop `-v1`). |
| LOW | Auth | Staged OAuth profile key `openai-codex:codex-cli` now upstream-`@deprecated` (doctor warning only). Discovery is by `provider` field, still resolves. | `workspace.py:697` | `constants.ts:13` deprecated; `OPENAI_CODEX_DEFAULT_PROFILE_ID="openai-codex:default"` | Optional rename. Cosmetic. |
| LOW | Chat event stream | Stale upstream file-path references in comments (gateway-protocol package extraction). | `connection_pool.py:1101` + handshake docstring `:316-339` | `b1117d9862` (#87797) moved `src/gateway/protocol/` → `packages/gateway-protocol/` | Update the two path strings. |

---

## 3. What's safe (do not re-investigate — at 2026.5.28, not the beta)

- **Protocol handshake:** floor still **4**, `version.ts` relocated not deleted. Our `minProtocol:4/maxProtocol:4` (`connection_pool.py:397-398`) passes.
- **Device pairing / signed-device v2:** `buildDeviceAuthPayload` byte-identical to our `build_v2_connect_payload`; server tries v3 then **falls back to v2**; `connect.challenge`/`hello-ok` unchanged; `paired.json` still a flat Record (SQLite move reverted).
- **On-disk agent auth layout:** `agents/main/agent/auth-profiles.json` unchanged. "Key by agent ID" (`95343affbb`) is an *in-memory* prewarm cache refactor, not the on-disk resolver. **NOTE:** the live ChatGPT-OAuth path writes **only** `auth-profiles.json` (no `codex/auth.json`) at HEAD — the `feedback_openclaw_chatgpt_auth_two_files` memory may be outdated at this version; **verify before relying on it.**
- **Chat/agent event stream:** `logs-chat.ts` byte-identical; assistant `data.text` still cumulative; tool `start`/`result` intact (new `phase:"update"` harmlessly dropped).
- **Billing / usage:** `sessions.list` still `operator.read`, `{sessions:[{key,inputTokens,outputTokens,model}]}` byte-identical; `lifecycle/phase:"end"` billing trigger fires once per run.
- **Bedrock provider (primary path):** full provider block + both pinned model ids + `memorySearch{bedrock, titan-embed-text-v2:0}` validate; externalization unchanged from current pin.
- **Channels (telegram/discord/slack):** ids, per-account field names, `dmPolicy:"pairing"`, `channels.status` RPC, 6-part DM session key, `{provider}-pairing.json` member-link all unchanged.
- **Plugins:** `memory.{backend:builtin,citations:auto}`, prebake dep-scan, uid-0 trust, Playwright path; #87477 gate **skips bundled-origin**.
- **Bump SSOT resolver:** env-from-JSON injection intact; `meta.lastTouchedVersion` auto-follows; new-user path clones CDK base task def; Track-2 wiring intact.

---

## 4. The bump, step by step

### (a) Code/config changes — land FIRST, before any image tag moves
1. **[BLOCKER]** `config.py:633`: delete `"embeddedHarness": {}` (+ comment 630-632). Do **not** add `embeddedAgent`.
2. **[LOW, bundle]** `config.py:332`: `anthropic/claude-opus-4-6-v1` → `anthropic/claude-opus-4-6`.
3. **[MED, bundle]** Add `embeddedHarness`/`embeddedAgent` to the deploy-time strip in `apply_deploy_aux_mutations`.
4. **[LOW, optional]** `workspace.py:697`: `openai-codex:codex-cli` → `openai-codex:default`.
5. **[LOW, optional]** Fix stale comment paths at `connection_pool.py:1101` + handshake docstring.
6. **No changes** for: protocol/RPC, device pairing, channels, providers, memory block.

**Add a config round-trip CI test:** pipe `build_openclaw_config_dict()` through upstream's `OpenClawSchema.safeParse` at the **target tag**. The single check that would have caught `embeddedHarness` and any sibling strict-key drift.

### (b) SSOT / Dockerfile / resolver — **PR-1** (with the §4a fixes)
7. `openclaw-version.json`: `upstream`/`image`/`tag`/`full` → `2026.5.28`. **Do not touch `dev.tag`/`prod.tag` yet.**
8. `Dockerfile:81`: `FROM ghcr.io/openclaw/openclaw:2026.5.28`.
9. `Dockerfile:216`: `@openclaw/$P@2026.5.28` (confirm both published on npm first).
10. `vendor-sidecars.sh:28`: `OPENCLAW_VERSION="2026.5.28"`; `:27`: `NODE_VERSION="22.19.0"`.
11. Hygiene: `_FALLBACK_UPSTREAM_VERSION` in `openclaw_version.py` + `Dockerfile:159` comment.

### (c) Build the extended image
12. Merge PR-1 → `build-openclaw-image.yml` builds + pushes `isol8/openclaw-extended:2026.5.28-<shortsha>`. **Capture the exact tag.**

### (d) Roll EXISTING containers (Track-2, admin dashboard) — needs the backfill
13. **Strip the stale key** from the prod container's on-disk config via `config_patcher.delete_openclaw_config_path(owner_id, ["agents","defaults","embeddedHarness"])` (`config_patcher.py:534`) — **delete-path helper, NOT deep-merge** (patcher can't replace a subtree, `config_patcher.py:153`). For the single prod container, `DELETE`+`POST /debug/provision` (reprovision, EFS preserved) is the cleanest path.
14. **PR-2:** set `dev.tag` to `2026.5.28-<shortsha>`. Verify: `aws ecr describe-images --repository-name isol8/openclaw-extended --image-ids imageTag=2026.5.28-<shortsha>`. Merge → `deploy.yml` redeploys dev container + service.
15. **Queue the roll** via admin: `POST /api/v1/admin/container/queue-image-update` per-owner, or fleet via `owner_id="all"`. 60s worker applies via `resize_user_container`.
16. After dev verification, **PR-3:** copy `dev.tag` → `prod.tag`, deploy, backfill+roll the single prod container.

### (e) Provision-time path for NEW users
Automatic via the CDK base task-def bump (PR-2/PR-3): provision clones the base task def, backend reads it live per provision. New users get the corrected config.

---

## 5. Test plan (dev first, one container)
1. **Boot/config (blocker check):** fresh dev provision reaches healthy, no crash-loop, no `Unrecognized key` in `/isol8/dev/openclaw` boot log. **Primary gate.**
2. **Webchat:** streaming chunks + tool indicator + thinking + clean `done` (v4 handshake, cumulative-text).
3. **Inference:** Bedrock-managed chat completes; BYOK-anthropic with `-v1`→bare; ChatGPT-OAuth no `FailoverError` on first message.
4. **Channels:** pair Telegram, `channels.status` connected, DM → agent replies.
5. **Billing:** `sessions.list` token counts increment usage.
6. **Marketplace (if touched):** deploy one listing into dev buyer, agent boots.
7. **Plugins:** slack/discord load (no "unavailable" on send); Bedrock resolves.
8. **Desktop:** aarch64 DMG → pair → node-host connect + one exec-approval round-trip.
9. **Existing-container roll:** Track-2 update on a *pre-existing* dev container (with backfill) comes back healthy — the only test of the stale-on-disk path.

---

## 6. Rollout & rollback
**Fleet reality:** prod ≈ **1 container** (founders' org). Mixed-version window is tiny, but new provisions carry the new image the moment `prod.tag` flips — coordinate the prod backfill with the flip.

**Staged:** PR-1 (fixes + mechanics) → build → capture tag → PR-2 (dev) → §5 on fresh + rolled dev container → E2E gate → PR-3 (prod) → backfill + roll prod, watch boot logs live.

**Rollback if crash-loop:**
- **Fastest:** revert `dev.tag`/`prod.tag` to `2026.5.12-0654e00` → `deploy.yml` re-registers old base task def → re-roll Track-2. New provisions revert immediately.
- **EFS `.bak` caveat:** `.bak` restores another copy of the *same* config — if the crash is strict-key drift, `.bak` re-crashes. Real recovery = **delete the offending key on EFS** (delete-path patch via ECS exec) or roll the image back. Diagnose via `/isol8/<env>/openclaw` boot log first.
- **Cold-cache watch:** first boot may pull different MCP/npx versions than the warm EFS cache (PR #785 surface). 120s `startPeriod` + 5 retries *should* absorb a one-time rewarm on the 0.5 vCPU box — **unmeasured**; if it SIGKILL-loops on CPU, that's the cache, not the config.

---

## 7. Open questions / follow-ups

**Must resolve before merge:**
- **Stable vs beta (the master switch).** Plan assumes `2026.5.28` stable. Beta `2026.6.1-beta.2` → re-run protocol floor / full config round-trip / auth / plugin compat against that exact tag first.
- **Confirm `@openclaw/slack@2026.5.28` + `@openclaw/discord@2026.5.28` published on npm** — Dockerfile pins an exact version; build hard-fails if absent.

**Owned but unwired:**
- **The `embeddedHarness` existing-container backfill has no owner.** Helper exists (`config_patcher.py:534`); the *wiring* (does the update-service pre-roll call it?) does not. Single prod container → `DELETE`+`POST /debug/provision`. Multi-container fleet → fold the delete-path strip into the Track-2 pre-roll.

**Permanent fix:**
- Add a CI preflight to `build-openclaw-image.yml` asserting `Dockerfile:81` FROM tag == `@openclaw/$P@` pin == `openclaw-version.json#upstream` (byte-identical). Converts three hand-mirrored literals into one machine-enforced invariant; prevents the mislabeled-artifact outcome that poisons the admin version badge.

---

## Appendix A — Surfaces NO analyst opened (completeness critic; audit before trusting "no breaks")

The 11 surfaces covered the user's explicit list. The critic found 8 consumer surfaces with real upstream range churn that **no report opened** — "no breaks" for these is absence-of-evidence, not evidence-of-absence:

1. **Cron RPC + `CronPanel`.** `cron.list/add/update/remove/run/runs`. Range churn: `f634062f35` quarantine malformed persisted jobs, `ca87241289` strict stagger-string parsing, `d7aa368776` reject negative timeouts, `b779bdb5a0` schedule number coercion, `7a381b807e` preflight model fallbacks (#82887). **A Track-2 roll could silently quarantine existing persisted cron jobs the old image accepted → lost scheduled work + usage/billing gap, no CronPanel error.** Founders likely have cron jobs → **audit before rolling prod.**
2. **Admin RPC inspector** (`admin_service.py:386-444`). `agent.identity.get` / `config.get` (envelope-vs-plain shape, `:438-444`) / `skills.status` (`{skills:[]}`-vs-array, `:427`). Track-2-*independent* (admin hits them regardless). Unverified at HEAD; a response-shape change silently breaks the inspector. `test_admin_org_resolution.py` encodes the contracts.
3. **Control-UI WS proxy** (`control_ui_proxy.py:281`). A *second*, independent handshake path: relays the **container-served SPA's** connect frame verbatim, string-injects `<base>`/`__OPENCLAW_CONTROL_UI_BASE_PATH__`. If the control-ui HTML/asset layout changed at HEAD the panel silently breaks. Asserts `gw_challenge.event=='connect.challenge'`. Churn: `c923b07784`, `f7977fb102`, `817dca5ae9`.
4. **Hooks ingress internal entries** (`config.py:732-765`). Cross-field validators present at 2026.5.12 and satisfied, but `internal.entries.{command-logger,session-memory}` + `OPENCLAW_HOOK_TOKEN` interpolation unchecked at HEAD. Churn: `e72621e566`, `5f0bec4479` (#85007), `1760881574` (#48534).
5. **Friday/Atlas workspace-bootstrap seeding** (`friday_identity.py`). Depends on `reconcileWorkspaceBootstrapCompletionState` (`workspace.ts`), modified by `4159a11ea3` *"Fix stale bootstrap file breaking channel agent turns"* — exactly the Friday-seed × channel-DM seam. A fresh Friday could lose seeded prompts or break the first channel turn. Canonical order `[AGENTS,SOUL,TOOLS,IDENTITY,USER].md` must still match.
6. **Atlas `.lobster` workflows + `SKILL.md` seeds.** TypeBox migration removed the lobster ajv shim; `3aae25358e` tightened numeric-option validation. Skills subsystem massively refactored (`8eb03d81a0` et al.). 8 `.lobster` + 7 `SKILL.md` dirs unverified against HEAD. (`feedback_backend_dockerignore_strips_md` bit Atlas SKILL.md once.)
7. **Marketplace deploy-from-artifact slice re-merge** — see §2 MED row (denylist lets removed keys through to brick buyers). Consumes S3 artifacts from OLD seller containers against the NEW buyer schema.
8. **Integrations / MCP-servers CRUD** (`integrations.py` + `config_patcher` mcp_patch). Only the static paperclip entry was checked. MCP churn: `71d24f98a8` SIGKILL stuck stdio (#86739), `47eb2d48d4` scrub redirect headers, `6c918ca85f` inherit tool restrictions. Per `project_integration_mcp_and_reprovision`, a malformed `mcp.servers` shape already bricked the container once (Linear).

## Appendix B — Cross-surface risks (critic)

- **embeddedHarness × Track-2 × marketplace-slice × config-patcher deep-merge:** one #85341 root cause, three fix sites (config.py:633 / on-disk backfill / marketplace strip). Deep-merge *cannot* replace-a-subtree → backfill MUST use the delete-path helper. A fix that only edits config.py:633 leaves Track-2 + marketplace bricked.
- **Cron quarantine × Track-2 × usage-billing:** a silently-quarantined cron job = lost scheduled work + usage/billing gap, no error surfaced.
- **Friday-seed × workspace-bootstrap-reconcile × channel-agent-turns:** `4159a11ea3`'s whole point was the stale-bootstrap-vs-channel-turn interaction — the exact Friday × channel seam.
- **Bundled-origin bypass × channel/inference/plugins:** baked slack/discord + externalized bedrock load as bundled-origin → **bypass** the #87477 compat gate → version drift fails silently at runtime, not discovery. The bypass is a *safety gap* for catching drift, not a feature.
- **Bump-ordering × Dockerfile-drift × admin version badge:** three hand-mirrored literals (FROM, plugin pin, JSON) with no CI cross-check; a JSON-only edit poisons the admin `CURRENT_DEV_TAG` badge operators trust — drift invisible at the surface meant to detect it.
- **Desktop DMG lag × node-host command drift × exec-approval:** desktop CI is aarch64-only; a lagging DMG against a rolled container drifts on exec-approval shape (`80227005a0` normalized auto-mode is *beyond* the local checkout, toward the beta).

## Appendix C — Under-analyzed assumptions (critic)

- The bump **target (~beta) is beyond the analysis basis (2026.5.28)** — the master switch (above).
- The embeddedHarness **existing-container backfill wiring** is unverified (helper exists; does the pre-roll call it?).
- **No full-config boot round-trip** was run end-to-end against the target tag — recommended as a CI gate.
- **`meta.lastTouchedVersion` × staggered fleet:** a not-yet-rolled OLD container reading a NEW-version-stamped config could trip `future-version-guard.ts:31-33` during the serial 60s-cadence roll window.
- **Paperclip adapter protocol lockstep:** the deployed `openclaw_gateway` adapter sends its own bundled `PROTOCOL_VERSION` (checkout shows 3; prod must be 4). Confirm + lockstep if a future tag ever moves the floor.
- **EFS npx/uv/chromium cold-rewarm** on first boot of the new image may pull different package versions than the warm cache (PR #785 surface) — unmeasured against the healthcheck budget on the 0.5 vCPU box.
- **Catalog refresh decision (product/pricing):** upstream moved opus default to `claude-opus-4-8` (#87890); `bedrock_pricing.py` must move in lockstep with any catalog refresh. Unowned — defer unless 4.8 is needed.

---

# ADDENDUM — Round-2 Gap Audit (8 consumer surfaces missed by Round 1)

*Scope: surfaces with real upstream range churn that no Round-1 analyst opened. Target unchanged: `2026.5.12` → HEAD `689e8ec` (~2026.5.28). Every claim re-verified at file:line on both repos; verify-over-analyze applied; `is_real:false` dropped.*

## A1. Gap-audit verdict

**YES — two NEW confirmed-real breaks beyond the Round-1 `embeddedHarness` boot-blocker, plus one false-green test that masks one of them.**

Headline: **the `#85341` agent-runtime-key removal (`bb46b79d3c`) has THREE Isol8 consumption loci, not one.** Round 1 found locus (1), the config-builder boot-blocker. Round 2 found locus (2), the **marketplace deploy-from-artifact path — a SECOND distinct brick that fires on the buyer's NEXT "Hire" even after the Round-1 fix lands** — and locus (3), a **false-green unit test** that stays green while producing a bricking config. A fix that lands only the Round-1 `config.py` change leaves the marketplace path bricked. All three (plus the `e12a6d6a67` `systemPromptOverride` removal) must ship in **one PR**.

The remaining 6 surfaces (cron-rpc, admin-rpc, control-ui-proxy, hooks-ingress, atlas-lobster-skills, mcp-crud) are **bump-clean** — zero bump-introduced breaks. One additional MEDIUM **behavioral** regression (not a boot-blocker) was confirmed on Friday/Atlas bootstrap seeding.

## A2. New what-breaks rows — MERGE into §2

| Sev | Surface | What breaks | Our file:line | Upstream evidence | Fix |
|---|---|---|---|---|---|
| **HIGH** | Marketplace deploy-from-artifact | Deploy denylist `_STRIPPED_KEYS={model,channels,workspace,id,agentDir}` strips only 5 keys. Upstream removed 4 top-level keys from `.strict()` `AgentEntrySchema`: `embeddedHarness`, `embeddedPi`, top-level `agentRuntime`, `systemPromptOverride`. A seller's OLD-image artifact (e.g. agent-set `systemPromptOverride`) is deep-copied **verbatim** into the buyer's now-strict on-disk `agents.list[]` (config_patcher writes with `json.dumps` only, NO zod check). Buyer's next chokidar reload fails `safeParse` → **crash-loop**. | `catalog_slice.py:21` (denylist), `catalog_service.py:417-419` (verbatim deep-copy), `config_patcher.py:77` (json.dumps-only write) | `bb46b79d3c` (#85341) + `e12a6d6a67` (`systemPromptOverride`). Verified: all four present at top-level at v2026.5.12, GONE at HEAD (the lone HEAD `agentRuntime` is nested in `AgentModelRuntimeEntrySchema`). | **HEAD-keyed ALLOWLIST** at `catalog_service.py:417` after `deepcopy(slice_["agent"])` (enforces on the consumption side for every already-frozen immutable artifact). Min fallback: extend the strip to the 4 keys. **Bundle with Round-1 `config.py:633`.** |
| MED | False-green marketplace test | `test_deploy_file_merges_full_agent_entry_into_agents_list` asserts `entry["agentRuntime"]=={"maxTurns":50}` **survives verbatim**; docstrings name `agentRuntime`/`embeddedHarness` as "must survive". Stays GREEN after the bump while producing a bricking config — defeats the regression gate. | `test_catalog_service.py:179` (+ docstrings :100,:146-150) | same `#85341` removal | Flip :179 to assert the 4 removed keys are STRIPPED while valid fields survive; fix the docstrings. |
| MED (behavioral, NOT boot-blocker) | Friday/Atlas bootstrap seeding | `filterCompletedWorkspaceBootstrapFile` (NEW in range) strips root `BOOTSTRAP.md` from the **loaded** per-run context whenever `setupCompletedAt` is set — both seeds write it day-zero. Post-bump `BOOTSTRAP.md` survives on disk but is **no longer injected into Friday's first interactive-turn context**; SOUL.md doesn't fully restate the lost greeting / no-tools-on-open-turn guidance. | `friday_identity.py:657` + docstring; `atlas_seed/__init__.py:78` | `4159a11ea3` (#82463). Verified: `filterCompletedWorkspaceBootstrapFile` refs = 0 at v2026.5.12, 3 at HEAD (wired into `resolveBootstrapFilesForRun`). | Fold Friday's opening-turn guidance into an **always-loaded** file (`SOUL.md`/`IDENTITY.md` — both in `CRON_BOOTSTRAP_ALLOWLIST`, not gated by `setupCompletedAt`). **Do NOT drop `setupCompletedAt`** (re-exposes reconcile's repair-branch `rm`). ⚠️ See A6 Atlas escalation. |

## A3. Track-2 existing-container risks (rolling the founders' prod container)

| Surface | Silent loss on roll? | Detail / pre-roll action |
|---|---|---|
| **CRON jobs** ⭐ | **No silent loss for valid jobs — but active `jobs.json` IS rewritten in place on roll.** | Quarantine machinery (`src/cron/persisted-shape.ts`) is **BRAND-NEW** (verified missing@OLD/present@HEAD). On first load the new image quarantines malformed rows to a `jobs-quarantine.json` sidecar (preserved, NOT deleted) and rewrites active `jobs.json` via `saveCronStore`. **NET RISK LOW**: predicate is a strict SUBSET of OLD create-time validation → no validly-created founders' job is quarantined. ⚠️ **Cosmetic gap:** `CronPanel` shows NO quarantine signal → a quarantined row vanishes from `cron.list` with no banner. **Pre-roll:** back up `jobs.json` / snapshot `cron.list`; after roll grep log for quarantine warnings + check for sidecar. |
| **Friday/Atlas bootstrap** | **No data/job loss; GUARANTEED behavior flip** for the entire existing fleet (next interactive turn stops injecting `BOOTSTRAP.md`). | See A6 — Atlas first-activation session-key type must be resolved first (could escalate cosmetic→structural). |
| **Marketplace deploy** | **No loss on the roll; bricks on the NEXT Hire of an OLD immutable artifact.** | Ship the deploy-side allowlist fix BEFORE re-enabling Hire on rolled containers; optionally S3-scan slices to size blast radius. |
| Admin RPC / Control-UI / Hooks / Atlas .lobster / MCP CRUD | **No.** | Track-2-independent or self-healing; details in A4. |

## A4. Newly-confirmed-safe (stop worrying — zero bump-introduced breaks)

- **Cron RPC + CronPanel** — 8 RPC names + scopes unchanged; cron protocol-schema diff **additive-only** (every change a new `Type.Optional`, zero removed lines); payloads valid; `cron.update` null-clearing a pre-existing no-op.
- **Admin RPC inspector** — all four read descriptors present, `operator.read`, request/response envelopes byte-identical; 50/50 unit tests pass. (Two PRE-EXISTING bugs in A6.)
- **Control-UI WS proxy** — `ui/index.html` byte-identical; `connect.challenge`/`hello-ok` identical; pairing gate pre-existing. Surface dead in prod anyway.
- **Hooks ingress** — top-level hooks zod block byte-identical + `.strict()`, no new required key; `${OPENCLAW_HOOK_TOKEN}` interpolation unchanged; new allowlist a verified no-op; re-register backfills `hook_token` (soft 401, not a crash).
- **Atlas .lobster + SKILL.md** — lobster numeric-option validator gates the lobster TOOL's params not `.lobster` file syntax (0 matches in Atlas files); skills refactor is pure relocation; seed uses `write_if_missing` (O_EXCL) so a roll never overwrites.
- **MCP-servers CRUD** — `McpServerSchema` still `.catchall(z.unknown())`, command/args/env intact; diff additive-only (optional `codex`). **Bonus:** the planned per-server `connectionTimeoutMs` band-aid for the bad-MCP-blocks-run issue is **schema-accepted AND runtime-honored at both refs** — ship it if desired.

## A5. Added checklist items

**Into §4d (existing-container roll):**
1. ☐ Ship the marketplace deploy-side allowlist fix **before** re-enabling any marketplace Hire on a rolled container. Bundle in the same PR as Round-1 `config.py:633`.
2. ☐ Snapshot `cron.list` + back up `jobs.json` for the founders' container before the roll; after, grep log for quarantine warnings + check `jobs-quarantine.json` row-count.
3. ☐ **Resolve Atlas's first-activation session-key type on dev BEFORE the prod roll** (capture the `sessionKey` on Atlas's first real run). If interactive/wake-keyed, the filter regression suppresses Atlas's structural activation (6 cron jobs + webhooks + security audit) — escalate the fix.
4. ☐ Plan an existing-fleet delivery path for the Friday/Atlas guidance fix — seeds are `write_if_missing`, so a code-only fix reaches ONLY fresh provisions; the founders need a Track-1 config-patch / targeted `SOUL.md` refresh.

**Into §5 (test plan):**
5. ☐ Flip the false-green test (`test_catalog_service.py:179` + docstrings) as part of the marketplace fix.
6. ☐ Post-bump smoke on a 2026.5.28 container: `lobster run vet-walk.lobster` (closes the @clawdbot/lobster-internals residual).
7. ☐ Deploy-merge brick regression test: a slice carrying `systemPromptOverride`/`embeddedHarness`/`embeddedPi`/top-level `agentRuntime` produces a buyer entry **stripped** of all four.

## A6. Still-open / residual

- **Atlas first-activation session-key type (gates the Friday/Atlas severity call).** Atlas's `BOOTSTRAP.md` is a STRUCTURAL one-time activation (6 cron jobs + webhooks + security audit), not a greeting. If its first activation runs under a **non-cron** session key, the new filter suppresses it entirely — structural, not cosmetic. Not provable from the OpenClaw checkout (Paperclip-adapter-determined). **Resolve empirically on dev before the bump.**
- **@clawdbot/lobster internals (2026.4.6 → 2026.5.22).** The `.lobster` schema validator lives in this external npm package (not vendored) → a strict tightening can't be ruled out by reading OpenClaw alone. Risk LOW (runtime tool error in one recruiter workflow, never a boot-block). Closable with the A5 smoke test.
- **Marketplace contaminated-artifact prevalence.** Unknown how many published slices carry one of the 4 removed keys. A one-time S3 scan sizes the blast radius; the fix ships regardless (immutability makes any present bad key permanent).
- **Two PRE-EXISTING admin-router bugs (bump-neutral — do NOT block the bump).** Verified broken identically at both refs: (a) `admin.py:551` sends `sessions.clear` — never a registered RPC (real: `sessions.cleanup`/`reset`/`delete`/`compact`); (b) `admin.py:512` sends snake_case `{agent_id}` but `AgentsDeleteParamsSchema` needs camelCase `agentId` (`additionalProperties:false`) → the admin "delete agent" button is 100% broken in prod today. Optionally fix in the same PR.
- **Control-UI re-enablement (out of scope while dead).** If `gateway.controlUi.enabled` ever flips true, the bump's device-pairing churn (#81288/#81289/#85459) goes live and the backend→container `remote` locality would hit `reject-control-ui-insecure-auth`. Needs its own audit then.

## A7. Generalized hardening (optional, defends every config-mutating surface at once)

`config_patcher.py:77` validates only `json.dumps` serializability, **not** the upstream zod schema — the shared root cause that lets BOTH the marketplace bad-key merge AND any future schema-removal mismatch reach EFS and brick at OpenClaw reload rather than at write. Adding a zod-equivalent strict gate to that locked-RMW write path would catch this entire class at EFS-write time. Directly addresses `reference_openclaw_config_brick`.
