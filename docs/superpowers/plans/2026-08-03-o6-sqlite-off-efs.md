# O6 — OpenClaw SQLite off EFS: local disk + Litestream→S3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all hot OpenClaw SQLite state from EFS (NFS, forced `journal_mode=DELETE`, whole-file locks) onto container-local Fargate ephemeral disk (WAL returns automatically), with Litestream v0.5.15 replicating to a per-env S3 bucket and restore-on-boot for durability. EFS keeps only the durable spine (workspaces, config, credentials, devices, caches).

**Architecture:** `OPENCLAW_STATE_DIR=/openclaw-local` (real local dir) + `OPENCLAW_CONFIG_PATH=/home/node/.openclaw/openclaw.json` (EFS) + `cron.store` pinned + reverse-symlinks for durable stateDir subdirs. Boot: migrate-or-restore → integrity check → `litestream replicate -exec "<doctor && gateway>"` as the supervised chain. Rollout is per-env via a new `storage` field in `openclaw-version.json` consumed at CDK synth (same pattern as `dev.tag`/`prod.tag`) — prod templates stay byte-identical until the promotion PR.

**Tech stack:** ECS Fargate (Spot), EFS, S3 + gateway VPC endpoint (exists), Litestream v0.5.15, AWS CDK (TypeScript), FastAPI backend, OpenClaw fork tag v2026.7.2-isol8.2 (NO fork changes needed).

## Evidence base (Phase 1 discovery, all adversarially verified)

Reports with file:line citations (scratchpad, session `c33c162a`): `spike-session-store.md`, `sqlite-inventory.md`, `isol8-layout.md`, `litestream.md`, `infra-s3-iam.md`, `baseline-lock-contention.md`. Load-bearing verified facts:

1. **Why EFS melts**: 7.2 probes the DB path's filesystem; NFS (statfs `0x6969` / mountinfo `nfs*`) forces `PRAGMA journal_mode=DELETE` (`src/infra/sqlite-wal.ts:16,296-312,346-357@v2026.7.2-isol8.2`). Every write = exclusive whole-file lock + journal fsync over NFS; `busy_timeout` 5000 ms (hardcoded, `openclaw-state-db.ts:76`) then "database is locked". Prod signature (7d trailing: 4–20 lines/day): transcript-index reconcile, prompt persist, auto-compaction, terminal-session persistence — all the **per-agent DB**; lane stalls 158–240 s ending in "Embedded agent failed before reply".
2. **The complete hot set** (everything else is legacy-migrated into these at boot): shared `state/openclaw.sqlite` (`openclaw-state-db.paths.ts:34-41`), per-agent `agents/{id}/agent/openclaw-agent.sqlite` (`openclaw-agent-db.paths.ts:20-31`, dynamic set — grows on hires), `state/openclaw-quarantine.sqlite`. Gateway lock already in `os.tmpdir()`.
3. **Symlinks do NOT restore WAL** (both fork verifiers, independently): the journal policy unions the **lexical** path against `/proc/self/mountinfo` (`sqlite-wal.ts:299,250-264,203-210`); EFS mounted at `/home/node/.openclaw` is a lexical ancestor → rollback wins even when the real file is local. `session.store` config = split-brain (5+ consumers open the default path: `task-registry.maintenance.ts:293`, `chat-tool-titles.ts:129`, `memory-search.ts:328`, `openclaw-state-lease.ts:187,204`, `session-cost-usage.ts:131-133`) and can't move the state DB at all. **The only correct no-fork lever: `OPENCLAW_STATE_DIR` pointing at a genuinely local path** (`paths.ts:69`; original==canonical==local → WAL).
4. **What `OPENCLAW_STATE_DIR` drags along** (must be pinned back to EFS): `openclaw.json` (`paths.ts:158-166` → pin via `OPENCLAW_CONFIG_PATH`), `credentials/` (`paths.ts:314-322`), `identity/` (`device-identity.ts:41`), `devices/`, `extensions/` (user plugin installs, `plugins-uninstall-command.ts:77`), cron store **key** (`resolveConfigDir` prefers `OPENCLAW_STATE_DIR`, `src/utils.ts:62-69`; rows keyed by resolved `cron/jobs.json` path string, `cron/store.ts:37-63` — pin via config `cron.store`, declared strict-safe at `zod-schema.root-shape.ts:411-414`, threaded at `server-cron.ts:209`). NOT dragged: workspaces (absolute EFS paths in openclaw.json, `config.py:758,773,1375`), `~/.openclaw/agent` (own env var, `src/agents/config.ts:123-128`), XDG/gog/.aws/mcporter/lobster/npm-cache/CLAWHUB (env-pinned to `~/.openclaw`, `container-stack.ts:622-666`, `Dockerfile:201`).
5. **Markers**: sqlite session markers embed the **absolute lexical store path** (`sqlite-marker.ts:13-15`) and take precedence over config (`session-accessor.transcript-target.ts:161-170`) — migrated DBs need a one-time marker string rewrite or pre-cutover history reads as empty. Agent DBs are also path-registered in the state DB on open (`openclaw-agent-db.ts:83-88`).
6. **Backend writes container SQLite over EFS (C8)**: `workspace.py:903-950` raw-DELETEs `auth_profile_store` (callers `billing_service.py:1487`, `container_auth.py:123`) — required because an **empty persisted store row shadows the oauth.json import** (merge only fires when the row is absent: `store.ts:920-940`; `models.authLogout` writes back an emptied-but-present row: `profiles.ts:180-222`). `friday_identity.py:180-183` probes the state DB on EFS (heal branch). Both break silently under O6 → sentinel design below.
7. **Litestream v0.5.15** (pin justified vs 0.3.14 one-way door; tracker sweep in report): `restore -if-db-not-exists -if-replica-exists` exits 0 on cold start (`restore.go:100-102,145-150@v0.5.15`); `replicate` does NOT auto-restore; SIGTERM in `-exec` mode forwards to child, waits, then final replica sync (`main.go:145-193`, `db.go:816-845`); dir-form config (`dir:`+`pattern:`+`recursive:`+`watch: true`) discovers **new DBs at runtime** (`replicate.go:212-224`, `main.go:710,821`) — covers mid-run hires; deploy overlap maintainer-tested safe but our posture (single-writer `min=0/max=100`, `ecs_manager.py:87-100,933`) avoids it entirely; known-open: #1171 idle-PUT pathology (~480 PUT/hr), #1220/#1164/#1309 restore-correctness (→ mandatory post-restore `integrity_check` + measurement), #1197 local staging growth (→ watch staging dir size).
8. **Infra**: S3 gateway endpoint exists (`network-stack.ts:52-54`); per-owner task roles exist for ALL providers (`ecs_manager.py:2292-2300`) but the `OwnerBedrockBoundary` has no `s3:*` (`container-stack.ts:289-342`) — boundary statement (CDK) + **separate unconditional** inline policy in `ensure_owner_bedrock_role` (NOT `build_owner_bedrock_policy`: skipped under `skip_bedrock_grant`, swapped by break-glass — `bedrock_aip.py:573-584,617-637`). Per-user task defs clone the CDK base (`ecs_manager.py:470-582`); **`resize_user_container` (= Track-2 apply) rebuilds env from the CDK base and DROPS per-user env/secrets** (`ecs_manager.py:1319-1359` — only hook_token + task role self-heal); restart path rebuilds fully (`:2404-2440`). Prod deploys in the same push gated on dev (`deploy.yml:1626-1641`) — per-env synth gate is the established prod fence (`container-stack.ts:601-614`).
9. **Secrets caveat**: `auth_profile_store` (OAuth tokens/BYO keys) lives INSIDE the per-agent hot DB (`auth-profiles/sqlite.ts:36-63`) → the S3 replica carries live credentials. Bucket: SSE-S3 + BLOCK_ALL + per-owner-prefix IAM (matches all 5 existing buckets; EFS today uses the CMK — **flagged delta, owner may veto to SSE-KMS**).

## Global constraints

- **NO fork changes.** Everything lands in isol8 repo (backend, CDK, extended-image Dockerfile).
- **Prod-inert until promotion**: every merged PR must leave `isol8-prod-*` CloudFormation templates **byte-identical** (prove with `cdk synth` diff per `reference_cdk_refactor_prove_template_byte_identical`) and backend behavior unchanged for old-layout containers. Gating is data-driven per-env synth (`openclaw-version.json` per-env `storage` field + per-env `dev.tag`), never code flags.
- **Both-layout backend compatibility** until prod promotes: `config.py`/`workspace.py` writes must be no-ops or equivalent on old-layout containers.
- Local state root: **`/openclaw-local`** (top-level, outside `/home/node` — no EFS lexical ancestry; disk-backed 20 GiB Fargate default, `/tmp` precedent at `container-stack.ts:471-473`).
- Replica prefix: `s3://isol8-{env}-openclaw-replicas/{owner_id}/<db-rel-path>` — no per-task uniqueness.
- Worktrees off origin/main; Codex review each PR; merge pre-authorized on approval; watch CI after every push. `gh pr checks` staleness + queued≠blocking caveats apply (memory).

---

### Task/PR 1: Fix per-user env/secrets loss on resize/Track-2 re-register (independent bug + O6 prerequisite)

**Files:** Modify `apps/backend/core/containers/ecs_manager.py` (`resize_user_container` ~:1287-1373); Test `apps/backend/tests/unit/containers/test_ecs_manager.py`.

**Verify first** (evidence, not memory): reproduce in a unit test that `resize_user_container` registers a revision WITHOUT `AWS_BEARER_TOKEN_BEDROCK`/BYOK secrets for an owner that had them. If confirmed → fix: rebuild `environment_for_task`/`secrets_for_task` from row state exactly as the restart path does (`:2404-2440` is the reference implementation — extract/reuse its provisioning-context build). If disproven → document why and drop the task (O6 then still needs ISOL8_OWNER_ID to survive: piggyback the same mechanism).

**Steps:**
- [ ] Write failing test: seed containers/billing rows for a bedrock owner with hook token + bearer env; call `resize_user_container(new_image=...)`; assert registered task-def container 0 env contains `AWS_BEARER_TOKEN_BEDROCK` and `ISOL8_OWNER_ID` absent-yet (added in PR2)
- [ ] Run test, confirm current behavior (red = bug confirmed)
- [ ] Implement env/secrets rebuild in resize path (reuse restart-path context builder; keep name-guarded `openclaw` container targeting per `refresh_task_definition` precedent `:687,699`)
- [ ] Run backend suite (`uv run pytest tests/ -v` scope per CI); commit; PR; Codex review; merge on approval

### Task/PR 2: Backend both-layout groundwork (all no-ops on old layout)

**Files:** Modify `apps/backend/core/containers/config.py` (cron pin, ~:1676 region), `apps/backend/core/containers/ecs_manager.py` (`_build_per_user_environment` :182-198), `apps/backend/core/containers/workspace.py` (`clear_default_agent_auth_profile_row` :890-984), `apps/backend/core/services/billing_service.py` (:1487 caller), `apps/backend/core/services/container_auth.py` (:123 caller), `apps/backend/core/services/friday_identity.py` (:180-183 comment only); Tests in `apps/backend/tests/unit/`.

**Interfaces produced:**
- `openclaw.json` gains `"cron": {"store": "/home/node/.openclaw/cron/jobs.json"}` — byte-equal to today's resolved default → zero behavior change old-layout; pins cron row keys forever (evidence fact 4).
- Per-user env gains `ISOL8_OWNER_ID={owner_id}` (unread by old layout).
- `clear_default_agent_auth_profile_row` becomes layout-aware: if the EFS DB path exists → today's DELETE (old layout unchanged); else → write sentinel `~/.openclaw/o6-clear-auth-profiles.json` (`{"agent_ids": ["main"], "requested_at": iso}`) to EFS and return `"restart_required"`; both callers on that return trigger a container restart via the existing restart lever. Sentinel is applied+deleted by the O6 entrypoint (Task 4) before gateway start.

**Steps:**
- [ ] Failing tests: config dict contains the cron pin; per-user env contains ISOL8_OWNER_ID; clear function old-layout path unchanged; clear function missing-DB path writes sentinel + returns restart_required; callers restart on that signal
- [ ] Implement; verify old-layout no-op property explicitly in tests (rendered openclaw.json for a fixture owner: only the `cron` key added)
- [ ] Full backend suite; commit; PR; Codex review; merge on approval

### Task/PR 3: CDK per-env storage gate + bucket + IAM + backend service env (prod byte-identical)

**Files:** Modify `openclaw-version.json` (+`dev.storage`/`prod.storage`, both `"efs"` initially; schema file), `apps/infra/lib/stacks/container-stack.ts` (storage-gated: bucket `isol8-{env}-openclaw-replicas` SSE-S3/BLOCK_ALL/RETAIN-prod-DESTROY-dev + request metrics; boundary S3+ListBucket statement; task-def env `OPENCLAW_STATE_DIR=/openclaw-local`, `OPENCLAW_CONFIG_PATH=/home/node/.openclaw/openclaw.json`, `LITESTREAM_S3_BUCKET`; new startupCommand branch calling `isol8-o6-boot.sh`), `apps/infra/lib/stacks/service-stack.ts` (storage-gated `LITESTREAM_BUCKET` env for backend), `apps/infra/lib/stacks/observability-stack.ts` (storage-gated S3 PutRequests alarm).

**Constraint:** with both `storage` fields `"efs"`, `cdk synth` output for BOTH envs must be byte-identical to pre-PR synth — capture both diffs in the PR description.

**Steps:**
- [ ] Add per-env `storage` field + type; thread into container/service/observability stacks like the tag gate (`container-stack.ts:601-614` pattern)
- [ ] Implement gated resources/env/startupCommand branch (content per Task 4's script contract)
- [ ] `pnpm --filter @isol8/infra synth` (or repo equivalent) before/after → diff both env templates → byte-identical proof in PR
- [ ] Commit; PR with synth-diff evidence; Codex review; merge on approval

### Task/PR 4: Extended image — Litestream binary + O6 boot script (backward-compatible image)

**Files:** Modify `apps/infra/openclaw/Dockerfile` (install litestream v0.5.15 pinned by sha256; ADD `isol8-o6-boot.sh` + `isol8-litestream.yml.tmpl`); Create `apps/infra/openclaw/o6/isol8-o6-boot.sh`, `apps/infra/openclaw/o6/isol8-litestream.yml.tmpl`.

**Boot script contract** (invoked by the gated startupCommand AFTER the existing browser/npx/uv/mcporter steps, replacing steps C3+D; `set -euo pipefail`, fail-loud `|| exit 1` idiom per `container-stack.ts:489-503`):
1. `mkdir -p /openclaw-local`; resolve `OWNER_ID` from `$ISOL8_OWNER_ID`, fallback `jq -r .owner_id ~/.openclaw/isol8-facts.json`; fail if neither.
2. Reverse-symlink durable spine into the local state root: for `credentials identity devices extensions`: `mkdir -p ~/.openclaw/<d> && ln -sfn ~/.openclaw/<d> /openclaw-local/<d>` (dir symlinks are safe for atomic file writes inside; no SQLite behind them → journal policy untouched).
3. **Restore-or-migrate** (idempotent):
   - Enumerate S3: `aws s3api list-objects-v2 --bucket $LITESTREAM_S3_BUCKET --prefix $OWNER_ID/ --query ... ` → derive replicated DB rel-paths.
   - For each: `litestream restore -if-db-not-exists -if-replica-exists -o /openclaw-local/<rel> s3://$LITESTREAM_S3_BUCKET/$OWNER_ID/<rel>`; then `sqlite3 <db> "PRAGMA integrity_check"` must return `ok` else exit 1.
   - **First-cutover migration** (S3 prefix empty AND `~/.openclaw/state` or `~/.openclaw/agents` exists on EFS): `cp -a` `state/` and `agents/` into `/openclaw-local/` (single-writer guarantees no concurrent writer; DELETE-journal = no `-wal` sidecars); marker rewrite on every copied DB: `UPDATE` the enumerated marker/path columns replacing `/home/node/.openclaw/` → `/openclaw-local/` (enumerate columns from fork evidence: session-entry `sessionFile` markers, cost-usage marker rows, state-DB agent-DB registration rows — pin exact table.column list during implementation from `sqlite-marker.ts` writers + `openclaw-agent-db.ts:83-88` registration; add a test fixture DB); `integrity_check` each; delete stale legacy `workspace-*/workspace-state.json` (replicates the friday-heal exactly — state DB present by construction); then `mv ~/.openclaw/state ~/.openclaw/pre-o6-archive/state` and same for `agents` (mv-aside, preserved).
4. Apply auth sentinel if present: `sqlite3 /openclaw-local/agents/<id>/agent/openclaw-agent.sqlite "DELETE FROM auth_profile_store WHERE store_key='primary'"` per listed agent; `rm` sentinel.
5. Render `/openclaw-local/litestream.yml` from template: explicit `state/openclaw.sqlite` + `state/openclaw-quarantine.sqlite` entries; `dir: /openclaw-local/agents, pattern: "openclaw-agent.sqlite", recursive: true, watch: true`; global replica `s3://$LITESTREAM_S3_BUCKET/$OWNER_ID/`; `addr: "127.0.0.1:9090"` (metrics, E2E measurement).
6. `exec litestream replicate -exec "sh -c 'timeout --kill-after=30 300 node /app/openclaw.mjs doctor --fix --yes --non-interactive < /dev/null || echo doctor-skip; exec node /app/openclaw.mjs gateway --port 18789 --bind lan'" -config /openclaw-local/litestream.yml`

**Steps:**
- [ ] Write script + template with a local shell test (`bats` not in repo — plain `sh -n` + a docker-run smoke against a scratch dir with a fixture sqlite DB exercising: cold start, restore path, migration path incl. marker rewrite, sentinel apply)
- [ ] Dockerfile: install litestream (pinned URL + checksum), COPY script/template, keep image fully backward compatible (nothing auto-runs)
- [ ] Push → `build-openclaw-image.yml` builds tag; watch run; note tag (promotion is PR5)
- [ ] Commit; PR; Codex review; merge on approval

### Task/PR 5: Backend Litestream IAM + flip DEV

**Files:** Modify `apps/backend/core/billing/bedrock_aip.py` (`ensure_owner_bedrock_role` — unconditional `owner-litestream-s3` inline policy scoped `arn:aws:s3:::$LITESTREAM_BUCKET/{owner_id}/*` + prefix-conditioned ListBucket, gated on `settings.LITESTREAM_BUCKET` non-empty), `apps/backend/core/config.py` (setting), tests; then `openclaw-version.json` (`dev.storage: "local-sqlite"`, `dev.tag: <PR4 tag>`).

**Steps:**
- [ ] Failing test: role ensure puts the S3 inline policy for bedrock AND chatgpt_oauth AND revoked owners when bucket set; skips when unset
- [ ] Implement; backend suite; commit (same PR or PR5a)
- [ ] **BEFORE flipping dev**: run the Phase 3 baseline load capture on dev's current EFS layout (see Phase 3 §baseline)
- [ ] Flip dev fields; PR with dev-synth diff (prod byte-identical proof again); Codex review; merge; watch `deploy.yml` dev jobs AND confirm prod jobs are CFN no-ops
- [ ] Dev fleet: Track-1 config patch adds `cron.store` pin to existing dev openclaw.json; then force-apply/restart lever re-registers + restarts dev containers onto the new revision

### Task 6 (Phase 3): Dev E2E — prove, don't assert

Via Claude-in-Chrome on dev.isol8.co with fresh `+clerk_test` accounts (fixed OTP). Evidence attached to PRs.

- [ ] **Baseline (BEFORE dev flip, part of PR5 sequence)**: scripted chat load (N concurrent messages + a forced compaction via long context) on an old-layout dev container; count marker lines (`database is locked|SQLITE_BUSY|ERR_SQLITE_ERROR|SqliteError`) in `/isol8/dev/openclaw`
- [ ] Fresh provision → chat works → exec `ls -la /openclaw-local/...`: WAL sidecar files present (`-wal`/`-shm`), `PRAGMA journal_mode` = wal; NO `state/`/`agents/` dirs on EFS; `integrity_check` ok
- [ ] Existing-container migration: pre-cutover dev account with history → post-flip restart → history visible (markers rewritten), cron rows intact (pin), Paperclip/Friday work
- [ ] Kill task mid-conversation (`aws ecs stop-task`) → task replacement restores from S3 → history/cron/plugin state recovered, OAuth intact (sentinel flow: reconnect ChatGPT OAuth → restart → re-import works)
- [ ] Restart + redeploy (force-apply) → no corruption (integrity_check), env intact post-resize (PR1 proof in the wild)
- [ ] Same load script post-cutover → marker-line count vs baseline (the point of the project)
- [ ] Idle ≥1h → S3 PutRequests count (request metrics/CloudWatch) ≈ 0; litestream staging dir size sane; mid-run hire → new agent DB starts replicating (watch:true proof)

### Task 7 (Phase 4): Prod promotion PRs — open, do NOT merge

- [ ] PR A: prod fleet Track-1 config patch runbook (cron.store pin) — documented, not executed
- [ ] PR B: `prod.storage: "local-sqlite"` + `prod.tag` bump (+ synth diff showing exactly the intended prod delta)
- [ ] Report: dev evidence, PR contents, rollout order (config patch → merge B → staged force-apply; mv-aside semantics `~/.openclaw/pre-o6-archive/`; loss statement: session JSONL compaction artifacts + gateway file logs + delivery-media become task-ephemeral; hard-kill RPO = sync-interval ≈1s unless replication silently stalled — mitigations: integrity-check-on-boot, PutRequests alarm, archive retained), then STOP for explicit go

## MEASURED EVIDENCE (2026-08-03/04 — supersedes every estimate in this doc)

Inside a live dev container, 200 individual commits (the transcript-append shape):

| configuration | ms/commit |
|---|---|
| EFS + DELETE journal — what runs today | **94.45** (independently re-measured 96.87) |
| local disk + WAL — the O6 target | **3.27**; on a real ECS ext4 volume **1.38** |
| EFS + WAL | 71.70 |
| local + DELETE | 5.71 |

`busy_timeout` is a hardcoded 5000 ms, so at 94 ms/commit a writer blows it behind ~53 queued
commits; at 3.27 ms, ~1500. **60% of prod agent-run lane failures over 7 days (18 of 30) are this
class.** Compaction timeouts and agent-loop failures are NOT and will survive O6.

Prod-inert proof: after #1579 merged, a real `deploy` run left **every prod CloudFormation stack
with an unchanged `LastUpdatedTime`** — the byte-identical synth claim verified end to end.

Cost: prod EFS today is **$1.02/month fleet-wide**. O6 adds ~$3.40/owner/mo of S3 requests, ~$7 if
litestream#1171's idle-PUT pathology bites (still OPEN; unmeasured for us — the prod flip is gated
on a measured idle window). `sync-interval: 10s` cuts the write-driven part 7.4× to $0.46.
The binding capacity constraint is the **20 GiB ephemeral pool** (~15 GB free after the image),
NOT the 300 s healthcheck startPeriod — measured restore headroom is ~12 GB.

## DESIGN DECISION: keep the state-root move; nested mounts REJECTED

A multi-agent review (workflow `wf_a4d430a2-2e7`) evaluated replacing this design with ECS
ephemeral volumes mounted *nested inside* the EFS mount at `~/.openclaw/{state,agents}`.

That design **works** — proven on real Fargate, two independent tasks: the nested mounts are real
ext4 (`statfs 0xef53`) inside the `nfs4` EFS mount, and OpenClaw's own shipped `sqlite-wal` bundle,
imported and called in-container, returned `wal` for them and `delete` for the EFS path in the same
process. It needs no env vars, no symlinks and **no path rewriting**.

It was rejected anyway: it *shadows* rather than moves the EFS content, and the backend keys
behavior off that path EXISTING — `clear_default_agent_auth_profile_row` routes to the O6 sentinel
only when the EFS DB is absent (`workspace.py`), `list_agents`/`agents_root` enumerate the EFS
agents dir, and `cleanup_agent_dirs` rmtree's it. Under the nested design all three silently operate
on a frozen ghost tree — OAuth reconnect breaks with no error. It would also still need the archive
`mv` (hence a fourth mount, since a mountpoint cannot be renamed), leaving a net saving of ~30 lines
against a silent, permanent failure mode. ECS also mounts in literal `mountPoints[]` order rather
than depth order, adding an undocumented ordering dependency.

Amendments the review DID earn, all shipped in #1585:
- `media`, `delivery-queue-media`, `logs` join the reverse-symlinked spine (the delivery queue's
  rows are replicated while its files would not be — a split-brain O6 would have introduced).
- The boot script honors `.o6-deleted` tombstones (#1582 writes them): deleting an agent's replica
  prefix while its container runs cannot be made race-free by waiting, so boot — when nothing is
  replicating that DB — is where the delete happens.
- `/openclaw-local` gets a config-less ECS volume (real ext4) instead of the overlayfs layer.

## Adversarial amendments (BINDING — supersede conflicting text above)

Three-attacker verification pass (backend sweep / mechanism / litestream sources at v0.5.15), full reports in workflow `wf_71c09f82-9ca` output. Every item below is a required change:

**A1 (BLOCKER) S3 replica lifecycle ownership.** Every wipe/reprovision lever (`routers/debug.py:271` delete_user_dir, `account_wipe.py:520-527`, dev-clean-slate-reset + prod-org-reprovision skills) must prefix-delete `s3://…/{owner_id}/` or the next boot restores pre-wipe state incl. OAuth refresh tokens — breaks the documented clean-slate reprovision and is credential retention after account deletion. Backend task role needs List/DeleteObject on the replicas bucket (service-stack, storage-gated). Update both skills' SKILL.md in the same PR.

**A2 (BLOCKER) Agent delete → S3 orphan resurrection.** `Workspace.cleanup_agent_dirs` (`workspace.py:584-623`; callers `websocket_chat.py:1980`, `catalog_service.py:656-660`) becomes a no-op on local-layout containers AND restore enumerates the S3 prefix — deleted agents' DBs re-materialize every boot, forever. Extend the same chokepoint to delete `s3://…/{owner_id}/agents/{agent_id}/` (uses A1 IAM).

**A3 (BLOCKER) Task 2 layout detection three-arm.** `clear_default_agent_auth_profile_row`: (1) EFS DB exists → today's DELETE; (2) `settings.LITESTREAM_BUCKET` set → sentinel + `restart_required` ONLY if a container is actually running (mirror `container_auth.py:61-67` guard) — restart async, never blocking `/poll`; (3) else → today's benign False (first-connect-during-onboarding path, `oauth.py:177-184`, stays a no-op on prod/old layout). `change_provider` maps `restart_required` → `_HotSwapUnavailableError` → existing cold path (`billing_service.py:1240-1400`, `restart: True`). Documented UX degradation: switches INTO chatgpt_oauth and settings reconnects bounce the container (switch-away stays hot via `models.authLogout`); E2E must cover reconnect-while-chatting.

**A4 Boot script §2 symlink list grows:** `credentials identity devices extensions skills npm git tools` (managed skills dir is configDir-derived — `skills/runtime/refresh.ts:101` — and CLAWHUB_WORKDIR writes `~/.openclaw/skills`: without the symlink every installed skill vanishes and future installs are inert; plugin npm/git installs `plugins/install-paths.ts:87-95`; tools `skills/runtime/tools-dir.ts:12` vs homedir-hardcoded npm prefix `install.ts:115-128`). Plus copy-if-exists `~/.openclaw/.env` → `/openclaw-local/.env` (file symlink unsafe under atomic replace; loaders `pre-bootstrap.ts:81-82`).

**A5 Boot script §3 marker rewrite is SCOPED, not blanket:** replace ONLY prefixes `/home/node/.openclaw/agents/` → `/openclaw-local/agents/` and `/home/node/.openclaw/state/` → `/openclaw-local/state/` (blanket replace destroys workspace paths embedded in `session_entries.entry_json`, `memory_index_sources/chunks.path`, plugin-index rows). Pinned rewrite targets: agent DB `session_entries.entry_json` (embedded `sessionFile` marker); state DB `agent_databases.path` (writer `openclaw-agent-db.ts:862-877`); state DB `managed_outgoing_image_records` only if media kept (default: accept loss, list in Task 7). NO cost-usage rewrite (markers computed at read time, `session-cost-usage.ts:346-351`). Fixture-DB test asserts the scoped rewrite + greps for leftover `/home/node/.openclaw/agents|state` strings.

**A6 Boot script §4 sentinel apply hardening:** skip when the agent DB file does not exist (sqlite3 on a missing path creates a junk DB then errors); tolerate `no such table: auth_profile_store` (parity with `workspace.py:910-913`); touch ONLY `auth_profile_store` (not `auth_profile_state`).

**A7 Boot script §5/§6 litestream config corrected shape:** there is NO top-level `replica:` key (non-strict YAML would silently ignore it). Use global inline replica-settings (`bucket`, `region`, `sync-interval`, propagated via `propagateGlobalSettings`, `main.go:339-349@v0.5.15`) + per-entry `replica: {type: s3, path: ...}`: state DBs as path-form entries with `path: {owner}/state/openclaw.sqlite` etc. **and `restore-if-db-not-exists: true`** (auto-restore at replicate start is real for path-form DBs, `replicate.go:196-203,470-516` — drop their boot-script restore calls); dir-form entry `dir: /openclaw-local/agents, pattern: "openclaw-agent.sqlite", recursive: true, watch: true, replica: {type: s3, path: {owner}/agents}` (dir-form is NEVER auto-restored → agent DBs keep the boot-script enumeration+restore). Use CLI `restore -integrity-check quick` instead of separate PRAGMA calls. Enumeration must be fail-loud and pagination-safe: S3 keys are `<db-path>/<%04x-level>/<txid>.ltx` — strip trailing two components; **a missed restore is silently destructive** (fresh empty local DB snapshots FORWARD over the replica, `db.go:1559-1642`). Static config with `$VAR` refs works (ParseConfig env-expands) — template render optional. Keep metrics bind 127.0.0.1 (mux also serves /debug/pprof).

**A8 Dev-flip rollout gap:** existing owners' task roles get the S3 inline policy only via `ensure_owner_bedrock_role`, which runs on the provision path only — the Task-5 force-apply wave would crash-loop the whole existing fleet on AccessDenied (litestream is the boot supervisor). PR5 adds a one-shot role-policy sweep over existing owners (re-runnable ops script in `apps/backend/scripts/`) run BEFORE the force-apply wave, per env.

**A9 Known accepted behaviors (state in Task 7 loss statement + E2E expectations):** OpenClaw's on-demand TRUNCATE checkpoints (DB close/eviction/shutdown, `sqlite-wal.ts:447,498`) busy-wait 5 s against litestream's persistent read lock and fail with a warn — recurring 5 s stalls on close paths, N×5 s serial at shutdown inside stopTimeout 120 (check for a config knob; else accept + measure); warn text does not match the baseline marker regex. SIGTERM final sync is SKIPPED if the gateway exits non-zero (`main.go:169-181`) — E2E asserts exit 0. Litestream plants `_litestream_seq`/`_litestream_lock` tables in every replicated DB. `media/outgoing` originals + delivery-queue-media + session JSONL artifacts + gateway file logs + `exec-approvals.json` become task-ephemeral; restored DBs may dangle references to lost JSONL (report symptom). `pre-o6-archive/` retains credential material on EFS — disconnect/switch-away flows no longer scrub it: add archive scrub to `delete_auth_profile_store` sweep or list as retention in Task 7. Device trust store (`device_pairing_paired`) now rides S3 too — strengthens the SSE-KMS-vs-SSE-S3 owner question. Debug EFS file-reader can no longer inspect `state/`/`agents/`; `pre-o6-archive/` is visible in the file viewer.

**A10 Synth-time assert:** `storage: "local-sqlite"` with tag `"bootstrap"`/unknown must fail synth (public bootstrap image lacks the boot script → crash-loop).

**A11 Confirmed viable as planned:** per-env synth gate (`app.ts:114-169`, stacks take `environment`); WAL-return for `/openclaw-local` paths incl. no-SQLite-behind-symlinks; watch:true discovers late-created DBs at any depth (basename pattern match, 250 ms debounce, dir-create rescan); restore CLI syntax + exit codes; `-exec` child inherits full env; backend consumer inventory complete (exactly two + the legacy sweep).

## Self-review notes
- Spec coverage: WAL-on-local (fact 3 → OPENCLAW_STATE_DIR), no-hot-SQLite-on-EFS (mv-aside + spine symlinks carry no SQLite), restore-on-boot (Task 4 §3), OAuth intact (C8 sentinel), baseline before cutover (Task 6 ordering inside PR5), no code-level gating (per-env synth field = existing dev.tag/prod.tag pattern), config.py both-layout (Task 2 no-op proofs), fork untouched.
- Open items pinned to implementation with evidence anchors: exact marker table.column list (fixture-DB test required), Codex-review loop per PR, SSE-S3 vs SSE-KMS owner veto flagged.
