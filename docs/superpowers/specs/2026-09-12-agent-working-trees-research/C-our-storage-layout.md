# Today's storage layout — inventory + coupling map

All paths below are relative to the container's `/home/node/.openclaw/` == backend's
`{EFS_MOUNT_PATH}/{owner_id}/` (per-user EFS access point root, chrooted). Line numbers
are current as of this checkout (branch `worktree-agent-a9efd55035be1f903`).

---

## 1. Container storage layout (CDK + boot script)

**Task definition** — `apps/infra/lib/stacks/container-stack.ts`
- `PER_USER_TASK_RESOURCES = { cpu: 1024, memoryMiB: 4096 }` (1 vCPU/4GB) at line 581,
  registered into `FargateTaskDefinition` at 583-589. Must stay in lockstep with
  `PER_USER_CPU`/`PER_USER_MEMORY_MIB` in `ecs_manager.py` (comment at 579-580).
- **No `ephemeralStorage` property is set anywhere** (confirmed by grep — zero hits for
  `ephemeralStorage`/`EphemeralStorage` in `container-stack.ts` or `ecs_manager.py`), so
  every per-user task runs Fargate's default **20 GiB** ephemeral storage pool. Comment
  at lines 1150-1151 confirms this is deliberate: "Draws from the same 20 GiB pool
  Fargate already includes, so it adds no cost." **This 20 GiB is the ceiling a
  114k-entry / multi-worktree workspace would compete against if moved to task-local
  disk** — nothing today reports usage against it.
- **EFS mount**: `openclaw-workspace` volume (EFS, IAM+TLS auth) mounted at
  `/home/node/.openclaw` (lines 1129-1134, 1164-1172). This is the durable spine:
  `openclaw.json`, `workspace-{agent}/`, `credentials/`, `devices/`, `skills/`, `media/`,
  `cron/`, `.aws/`, `lightbulb-facts.json`.
- **Two ECS-managed ephemeral (task-local, real ext4 on task NVMe) volumes, nested
  INSIDE the EFS mountpoint** (lines 1152-1162): `openclaw-state` → `.openclaw/state`,
  `openclaw-agents` → `.openclaw/agents`. Mount **order is load-bearing** (comment
  1136-1141): they're registered after the EFS volume so they *shadow* whatever EFS
  holds at those two paths. There is no longer a second EFS view (`/efs-raw` was
  retired — comment 1143-1148).
- `/tmp/openclaw-browser`, `/tmp/openclaw-npx`, `/tmp/openclaw-uv` — also task-local
  (Fargate disk-backed ephemeral storage, not EFS), created by the startup command
  (lines 661-754) and symlinked over the EFS-mounted `browser/`, `.npm`, `.cache/uv` to
  get chromium + MCP package extraction off NFS (rationale: chromium fsync/lock traffic
  and thousand-file npx/uv unpacks were the NFS-RPC-stall trigger; see comments
  599-660, 677-727). **This is the platform's own precedent for "move a
  write-heavy/many-small-file tree off EFS onto task-local disk"** — directly
  analogous to what a workspace-tree move would need to do.
- Boot command chain (lines 661-926): lock-recovery → browser/npm/uv symlink
  migrations → mcporter/xurl symlinks → Xvnc/warp/websockify (as root) → chown the two
  ephemeral volumes to 1000:1000 (root must do this because Docker materializes
  ECS-managed volumes root:root; comment 883-920) → `exec setpriv --reuid=1000
  ... /usr/local/bin/isol8-local-sqlite-boot.sh` (line 926).
- Health check (1075-1114): `/healthz`, 30s interval, 10s timeout, 6 retries,
  **300s startPeriod (ECS max)** — sized explicitly around EFS first-touch latency +
  the boot-time `doctor --fix` pass + cold MCP connect storm. A slow EFS tree op that
  blocks the event loop (see §2/§6) eats directly into this budget.

**local-sqlite boot chain** — `apps/infra/openclaw/local-sqlite/`
- `isol8-local-sqlite-boot.sh`: resolves `ISOL8_OWNER_ID` (task env, else
  `lightbulb-facts.json` fallback, line 63-67) → **asserts** `state/` and `agents/`
  report a local filesystem type (ext4/xfs/etc, not nfs) via `stat -f -c %T`, **fails
  closed** otherwise (lines 92-108: "Continuing would run SQLite on the network
  filesystem... Refusing to boot") → enumerates the owner's S3 prefix
  (`s3://$LITESTREAM_S3_BUCKET/$OWNER_ID/`) for tombstones + replicated DB paths →
  purges tombstoned agent prefixes (lines 139-163) → **restores every replicated DB
  concurrently, bounded to 6 in flight**, integrity-checked (`litestream restore
  -integrity-check quick`, lines 180-199) → applies a backend-staged
  `import-state/agents/*` hand-off if present (backup-import path, lines 218-281) →
  applies a `clear-auth-profiles.json` sentinel (lines 283-320) → `exec litestream
  replicate -config isol8-litestream.yml -exec isol8-local-sqlite-app.sh` (line 328).
- `isol8-litestream.yml`: replicates exactly two DB shapes to S3, **10s sync-interval**
  (down from litestream's 1s default — 7-agent owner was doing ~47 S3 PUTs/min at 1s,
  comment 34-38):
  - `state/openclaw.sqlite` → `s3://bucket/{owner}/state/openclaw.sqlite`
  - `agents/**/openclaw-agent.sqlite` (recursive, fsnotify `watch: true` — picks up a
    brand-new agent's DB mid-run with no restart) → `s3://bucket/{owner}/agents/...`
  - **`openclaw-quarantine.sqlite` is deliberately NOT replicated** (comment 22-32) —
    forced rollback-journal mode, litestream can't ship it.
  - Only files matching this pattern are replicated — anything else under `agents/{id}/`
    (e.g. legacy non-SQLite `sessions/` files) is task-local and **not backed up**.
- `isol8-local-sqlite-app.sh`: runs `openclaw doctor --fix` with a 300s timeout
  (measured 52-84s across 9 fleet boots; a shorter cap was tried and reverted because
  truncating mid-repair is worse than finishing late), then `exec node openclaw.mjs
  gateway --port 18789 --bind lan`. litestream supervises this as its `-exec` child;
  final replica sync only happens if the gateway exits 0 on SIGTERM.
- **On crash/OOM/replace**: task-local `state/`+`agents/` disk is gone; next boot
  restores from the S3 replica (≤10s of loss, per the sync-interval). **On graceful
  stop**: litestream final-syncs before exit. **Size limit**: none explicit — it's
  whatever fits in the 20 GiB Fargate ephemeral pool alongside `/tmp` scratch above.

**Net for the redesign**: SQLite state already made this exact migration (EFS → task-local
+ S3-backed sync) once, on 2026-08-04, for reliability (NFS rollback-journal lock
contention). The workspace-tree problem (git clones/worktrees) is the same shape of
problem one level up, and the codebase has a working, documented pattern to copy
(concurrent-bounded restore, integrity check, fail-closed filesystem-type assertion,
tombstone-on-delete, watch-based incremental replication) — see `isol8-local-sqlite-boot.sh`
in full.

---

## 2. Backend EFS access — narrative inventory

Every path below funnels through `core/containers/workspace.py`'s `Workspace` class
(singleton via `get_workspace()`, mount root `settings.EFS_MOUNT_PATH`). Its own
methods (`list_directory`, `read_file`, `write_file`, `write_bytes`,
`extract_tarball_to_workspace`, `cleanup_agent_dirs`, `delete_user_dir`, etc.,
`workspace.py` lines 129-790) are **plain synchronous filesystem calls** — the module
itself does no thread-offloading; that's each caller's job, and callers are
inconsistent about it (see the table in §2b).

- **`routers/workspace_files.py`** (356 lines) — file-viewer REST surface over
  `workspace-{agent_id}/`. `_collect_recursive` (101-121) does `iterdir()` + `stat()` +
  `resolve()` per entry, unbounded except `max_depth=10` (default), called directly
  (no `asyncio.to_thread`) from `list_workspace_tree` (196-229) when `recursive=true`.
  **This is the exact function that froze prod on 2026-09-12** — see the incident note
  in §6. `read_workspace_file`/`write_workspace_file`/`list_config_files`/
  `read_config_file` (232-357) also call `Workspace` methods directly on the event
  loop; none of this file imports `asyncio`. Needs only a **subtree** of one agent's
  workspace, never the whole user root.
- **`routers/brain.py`** (417 lines) — owner-facing CRUD over the memory-wiki vault at
  `wiki/main/` (written by the container's memory-wiki plugin). Writes/uploads/deletes
  ARE thread-offloaded (`asyncio.to_thread(_locked_write, ...)` line 204,
  `_locked_inbox_upload` line 362, `_locked_delete` line 411) and additionally
  file-locked (two-layer: in-process `threading.Lock` + `fcntl.lockf` sidecar via
  `core/services/efs_file_lock.py`). **But `list_brain_tree` (99-110) and
  `read_brain_file` (113-131) are NOT thread-offloaded** — same class of bug as
  workspace_files.py, just scoped to a smaller, single-vault subtree instead of an
  agent's whole working tree.
- **`routers/container_files.py`** — single `POST /files` endpoint, writes uploads to
  `workspace-{agent_id}/uploads/{name}` via `workspace.write_bytes(...)` called
  directly on the event loop (line 87-88), no thread-offload. Bounded to 10 files ×
  10MB, so low risk in practice.
- **`core/containers/workspace.py`** — see module docstring (lines 1-14) for the
  canonical path layout. Notable methods: `list_workspace_agent_dirs` (252-269, scans
  `workspace-{id}` dirs — "never a `workspaces/` folder, which upstream doctor treats
  as retired plugin state and deletes"), `cleanup_agent_dirs` (642-681, best-effort
  `rm -rf` of `agents/{id}` **and** `workspace-{id}` after an OpenClaw-side delete,
  because `agents.delete`'s own trash-move fails cross-device from EFS to the
  container overlay), `extract_tarball_to_workspace` (687-707, marketplace/catalog
  deploy target), template-sidecar helpers (709-757), `read_openclaw_config`/
  `read_cron_jobs` (759-787).
  - **Important subtlety** (see §6): under the local-sqlite layout, `agents/{id}/` is
    one of the two ECS-managed ephemeral volumes *inside the running container*, but
    the **backend's own EFS mount is not shadowed** — it sees the raw EFS filesystem at
    that path, which is either empty (new agent, DB never touched EFS) or stale
    pre-2026-08-04 legacy content. `clear_default_agent_auth_profile_row` (1183-1300)
    documents this exactly: it deletes a row from the **EFS-visible** copy of
    `agents/main/agent/openclaw-agent.sqlite`, then — `if settings.LITESTREAM_BUCKET` —
    treats that hot delete as informational-only residue and instead writes a
    `clear-auth-profiles.json` **sentinel** the boot script must consume, because "the
    EFS store this delete just edited is residue the running task never reads — its
    live store is on container-local disk" (comment 1292-1297). A prod incident
    (founder-org, 2026-08) already happened from getting this wrong. **Any workspace-tree
    move that puts `workspace-{agent}/` on task-local disk re-creates this exact
    class of bug for every backend function in §2b below that touches that path
    directly** unless a replacement RPC/sync path exists first.
- **`core/services/config_patcher.py`** (1418 lines) — the **only** chokepoint for
  mutating `openclaw.json`. `_locked_rmw` (98-177) resolves `{EFS_MOUNT_PATH}/{owner}/
  openclaw.json[.bak][.lock]`, acquires the two-layer lock (in-process `threading.Lock`
  keyed on the lock path + `openclaw_config_sidecar_lock`), reads, calls `mutate_fn`,
  gates against the OpenClaw JSON schema, backs up, atomically renames a tempfile in,
  chowns to 1000 — **and wraps the whole thing in `asyncio.to_thread(_do_rmw_locked)`**
  (line 177). Every other function in the file (`patch_openclaw_config`,
  `apply_deploy_aux_mutations`, `replace_provider_block`, `register_lightbulb_agent_account`,
  etc.) is a thin `mutate_fn` closure over this. **Scope: exactly one file (+ its `.bak`/
  `.lock` siblings) at the user root — never the workspace tree.** Fully orthogonal to
  the tree-walk problem.
- **`core/services/efs_file_lock.py`** — the shared two-layer NFS RMW-locking
  primitives `config_patcher` and `routers/desktop_pair.py` use. Defines
  `OPENCLAW_CONFIG_GUARD_SUFFIX = ".isol8-guard"` and
  `OPENCLAW_CONFIG_PROTOCOL_SUFFIX = ".isol8-protocol"` (lines 35-38) — the
  boot-recovery guard + protocol-transition marker referenced in CLAUDE.md's Config
  Patching Notes.
- **`core/services/agent_export.py`** (1678 lines) — seller-agent → marketplace
  tarball snapshotter. `_resolve_agent_dir` (429-478, docstring 432-446) is explicit
  about the split: `agents/{id}/` = OpenClaw runtime state (never tarred), `workspace-{id}/`
  = the seller's actual content (what's tarred). `_build_tarball` (534-676) is a
  **synchronous, unbounded `tarfile.add()` walk** of the whole agent-workspace dir —
  but its filter callback (562-659) already prunes exactly the class of thing that
  caused the 2026-09-12 incident:
  - any dir containing a nested `.git` (lines 591-599: "marks a repo the agent cloned
    to work on... not the agent. Prune the whole subtree") — **this is the platform's
    own existing precedent that a cloned-repo working tree is not part of an agent's
    portable/durable definition.**
  - runtime-scratch dirs by prefix (`_SCRATCH_DIR_PREFIXES = ("run-evidence",
    "run-artifacts", "run-scratch", "github-cache")`, line 170) and the `.run/`
    dot-dir convention (excluded by the blanket dotfile filter) — see §3.
  - hard caps: `_MAX_EXPORT_FILES = 2000`, `_MAX_EXPORT_BYTES = 50 * 1024 * 1024`
    (lines 320-321) — raises `ExportTooLargeError` mid-walk.
  - `_SKIP_DIR_NAMES` (133-146: `.git`, `.venv`, `node_modules`, `__pycache__`, caches,
    `uploads`, etc.) and `_SKIP_FILE_BASENAMES` (line 157-158).
  - Callers wrap this in `run_in_thread` explicitly **because it's synchronous** — see
    `routers/marketplace_listings.py` lines 1556-1567 ("The EFS walk + tar + scan is
    synchronous; run it in a worker thread so a large workspace can't stall the event
    loop for every other request") and `routers/agent_builder.py` 215-218.
- **`core/services/catalog_service.py`** (1088 lines) — `deploy_from_artifact`
  (394-...) extracts a purchased artifact's `workspace.tar.gz` into the buyer's
  `workspace-{new_agent_id}/` via `self._workspace.extract_tarball_to_workspace(...)`
  inside a nested `_extract()` closure, wrapped in `await asyncio.to_thread(_extract)`
  (line 492). Correctly thread-offloaded.
- **Provisioning writes** — `core/containers/ecs_manager.py` `write_user_configs`
  (4094-4157+) writes exactly three things to EFS per provision/reprovision:
  `openclaw.json` (via `write_openclaw_config`), `devices/.node-device-key.pem`,
  `devices/paired.json`. `core/containers/config.py` `write_openclaw_config`
  (2983-3090) and `read_openclaw_config_from_efs` (3093-3121) both wrap their blocking
  body in `asyncio.to_thread` (lines 3090, 3121) — the inner write itself delegates to
  `config_patcher`'s already-threaded RMW where applicable (comment at 3064: "inside
  `asyncio.to_thread` already"). **None of this touches `workspace-{agent}/`** — agent
  workspace content is created later, either by OpenClaw itself on first agent run, or
  by a catalog/company-agent deploy (`materialize_agent`/`deploy_from_artifact`).
- **`core/services/owner_facts.py`** — `write_owner_facts` (204-233) calls
  `get_workspace().write_file(owner_id, FACTS_FILENAME, ...)` and
  `.delete_file(..., _LEGACY_FACTS_FILENAME)` **directly on the event loop, no
  thread-offload** (single small JSON file, low risk in practice, but same pattern
  gap). Regenerates the entire file from DynamoDB (billing + agent-email +
  provisioning-workflow rows) every call — it's **fully derived, never merged**, so
  losing it costs nothing durable (see §2b).
- **`core/services/messaging.py`** — `store_image_attachment`/`load_image_attachment`
  (~727-770) write/read `media/messaging/attachments/{room_id}/{attachment_id}{ext,.json}`
  directly via `Workspace.write_bytes`/`write_file`/`_resolve_user_file`, called
  synchronously from `routers/messaging.py`'s `upload_room_attachment`/
  `get_room_attachment` (335-370) with **no thread-offload** (bounded to a 5 MiB
  image + tiny JSON sidecar, so low risk, but same gap in the general pattern).

### 2b. Path-pattern table (writer / reader / durability)

| Path pattern (relative to `{EFS}/{owner}/`) | Writes from | Reads from | Durable across task restart? | Key citation |
|---|---|---|---|---|
| `openclaw.json` | Backend only: `ecs_manager.write_user_configs`→`config.write_openclaw_config` at provision; every `config_patcher.py` mutator at runtime; `PATCH /debug/provision` (wholesale rewrite) | OpenClaw gateway (loads at boot, chokidar hot-reload); backend (`config_patcher._do_rmw` read, `workspace.read_openclaw_config`) | **Yes, must** — sole source of agent roster/provider config; not reconstructable without re-running provisioning | `config_patcher.py:136-177`; `ecs_manager.py:4094`; `config.py:2983` |
| `openclaw.json.bak` | Backend (`shutil.copy2` inside `_locked_rmw`) | Nobody automated (manual recovery only) | No — re-derived from the next successful write; pure safety net | `config_patcher.py:159` |
| `openclaw.json.lock`, `.isol8-guard`, `.isol8-protocol` | Backend + OpenClaw container, coordinated protocol | Backend + container (existence/lock state only) | No — pure lock-coordination sidecars, meaningless at rest | `efs_file_lock.py:35-39` |
| `workspace-{agent}/{IDENTITY,SOUL,USER,HEARTBEAT,TOOLS,MEMORY,AGENTS}.md` | Backend (`workspace_files.py` config-tab writes; `company_agent_materializer.write_file_if_missing` at deploy) **and** the OpenClaw agent itself (self-edits `MEMORY.md`/`SOUL.md` at runtime) | Backend (file viewer); OpenClaw agent (loads persona each session) | **Yes, must** — hand-authored + self-evolved identity/memory, cannot be regenerated | `workspace_files.py:152-160`; `company_agent_materializer.py:89-93` |
| `workspace-{agent}/` — arbitrary work product (docs, code the agent wrote) | OpenClaw agent | Backend (file viewer, marketplace export); OpenClaw agent | **Yes** — genuine authored content | `workspace.py:1-14` |
| `workspace-{agent}/` — nested git clones/worktrees, `node_modules`, caches | OpenClaw agent (coding-harness sessions) | OpenClaw agent only | **No** — the platform's own export code already prunes nested-`.git` subtrees wholesale as "not part of the agent's portable definition" | `agent_export.py:591-599`, `133-146` |
| `workspace-{agent}/uploads/` | Backend (`container_files.py POST /files`) | OpenClaw agent | Yes until consumed — no consumption signal exists, so must be treated as durable indefinitely | `container_files.py:87-88` |
| `workspace-{agent}/.template` sidecar | Backend only (`write_template_sidecar`/`mark_template_install_complete`) | Backend only (deploy provenance) | Low-value metadata — losing it only loses "installed from template X" display; rebuildable by re-deploying | `workspace.py:709-747` |
| `workspace-{agent}/skills/` | Backend (`company_agent_materializer.write_bytes` skill_assets; catalog deploy untar) | OpenClaw skill loader (`skills.load.extraDirs`); backend export enumerator | **Yes** — installed functionality, equivalent to code | `agent_export.py:1113-1134` |
| `media/messaging/attachments/{room}/{id}{ext,.json}` | Backend (`messaging.py` human upload) **and** container's `lightbulb-channel` plugin (agent-sent attachment) | Backend (serves GET); OpenClaw agent | **Yes, must** — canonical message bytes; DynamoDB holds only metadata/pointer, never the bytes | `core/services/messaging.py:727-763`; `plugins/lightbulb-channel/src/channel.ts:801` |
| `.config/`, `.local/share/` (XDG) | Backend `ensure_xdg_dirs` (mkdir only); container tools (gog, aws CLI) write actual content | Container tools | **Yes** — e.g. gog's OAuth file-keyring lives in `.local/share`; losing it logs the owner out of Google integrations | `workspace.py:98-108, 201-239`; `container-stack.ts:971-984` |
| `.cache/` (XDG) | Container tools | Container tools | **No** — pure cache | `workspace.py:98-108` |
| `.aws/config`, `.aws/lightbulb-creds.mjs`, `.aws/token` | Backend only (`aws_cli.py` materializer, on connector setup) | Container's `aws` CLI via `credential_process` + `aws-exec-profile` plugin | **Yes** for `config`/helper/`token` (re-running connector setup needed if lost) | `credentials/materializers/aws_cli.py:1-40` |
| `.aws/cache.json` | Container (`lightbulb-creds.mjs` helper, on each credential fetch) | Same helper | **No** — 10-minute credential cache, safe to lose | `credentials/materializers/aws_cli.py` (`_HELPER_TEMPLATE`) |
| `lightbulb-facts.json` | Backend only (`owner_facts.write_owner_facts`, best-effort, full rewrite every call) | OpenClaw agents (bundled `lightbulb` skill tells them to read it) | **No** — fully derived from DynamoDB billing/provisioning rows on demand; never merged, so it's a pure cache of that data | `owner_facts.py:1-16, 204-233` |
| `cron/jobs.json` | OpenClaw's own cron subsystem primarily; backend's catalog deploy remaps job ids into it when installing a purchased agent's schedule | Backend (`workspace.read_cron_jobs` for export/import); OpenClaw cron plugin | **Yes, must** — user/agent-configured schedules, no other source of truth | `workspace.py:769-787` |
| `credentials/oauth.json` (container-global) | Backend only (`pre_stage_auth_profile_store`) | OpenClaw's one-shot OAuth import into its (now task-local) SQLite auth-profile store | **Durable-until-consumed** — imported once into SQLite, then the EFS copy is inert residue | `workspace.py:822-864` |
| `opencode-data/opencode/auth.json` | Backend (bootstrap only, `overwrite=False`) **and** the OpenCode harness itself (rewrites on every token rotation) | OpenCode harness | **Yes, continuously** — unlike `oauth.json`, this is live-rewritten, not one-shot-imported | `workspace.py:931-1009` |
| `agents/*/agent/auth-profiles.json` (legacy per-agent) | Nobody writes new ones (pre-6.6 fan-out, retired) | Backend sweep only, on disconnect | **No** — dead path; also now shadowed by the task-local `agents/` ephemeral volume inside the running container (see §2 subtlety) | `workspace.py:1036-1072` |
| `state/openclaw.sqlite` | OpenClaw gateway process only | OpenClaw gateway only — **backend never touches this path** (confirmed: zero references in `apps/backend`) | **Yes** — task-local, but litestream-replicated to S3 every 10s | `isol8-litestream.yml:42-48` |
| `agents/{id}/agent/openclaw-agent.sqlite` | OpenClaw agent process | OpenClaw agent process; backend reads/deletes the **EFS-visible** copy only in the pre-cutover/legacy sense (see subtlety above) | **Yes** — task-local, litestream-replicated to S3 every 10s (watch-based, picks up new agents live) | `isol8-litestream.yml:49-58`; `workspace.py:1183-1300` |
| `agents/{id}/sessions/` (legacy, non-sqlite) | OpenClaw (legacy) | OpenClaw | **No** — not matched by litestream's `openclaw-agent.sqlite` pattern; task-local and unreplicated | `isol8-litestream.yml:49-52` |

---

## 3. What the container/skills tell the agent about repos, clones, worktrees

**There is no guidance anywhere in the bundled skills, plugins, or seed templates about
where to `git clone`, how to manage worktrees, or when to clean either up.** Specifically:

- `grep -rn "worktree" apps/infra/openclaw/skills/` — **zero matches** in either
  `lightbulb` or `paperclip` skill (`SKILL.md`, every `references/*.md`,
  `MAINTENANCE.md`). The only appearances of the substring "checkout" are Paperclip's
  *issue* checkout (`POST /api/issues/:id/checkout`, an API-level task-claim, unrelated
  to `git checkout`) and `issue-workspaces.md`'s "execution workspace" concept, which is
  a Paperclip service-orchestration abstraction (start/stop/restart dev servers for an
  issue) — not a git-clone/worktree location contract. Quoting the actual scope
  (`apps/infra/openclaw/skills/paperclip/references/issue-workspaces.md:17`): `cwd` /
  `branchName` are read back from the API as informational fields, never prescribed.
- `grep -rniE "git clone|gh repo clone" apps/infra/openclaw/skills/` — zero matches.
- The one existing convention that touches "what belongs in the workspace root" is the
  **`.run/` scratch-dir convention**, and it is about **marketplace-export cleanliness,
  not disk hygiene**:
  > "Working scratch and run evidence you keep for your own recall but do NOT intend to
  > ship... belong under `.run/` in your workspace: a dot-dir, so **it persists across
  > runs** but is excluded from any marketplace export of this agent."
  > — `apps/infra/openclaw/skills/paperclip/references/heartbeat.md:90`
  >
  > "Keep the root for files that ARE the agent (persona, `skills/`, `pipelines/`,
  > `memory/`) — write run scratch... under `.run/` (a dot-dir: **just as durable**, but
  > excluded from marketplace exports)."
  > — `apps/infra/openclaw/skills/lightbulb/references/platform-primitives.md:16`

  Both explicitly say `.run/` content is **just as durable / persists across runs** —
  the opposite of "safe to discard." It solves "don't leak scratch into a published
  listing," not "bound how much disk an agent's clones use."
- The starter-roster Engineering seed (`apps/backend/core/services/starter_roster/files/engineering/AGENTS.md`)
  is one line: `"Build, test, operate, and report evidence. Do not assume ACP is
  required."` — no repo-location or cleanup instruction.
- The four baked plugins (`aws-exec-profile`, `lightbulb-channel`, `lightbulb-marketplace`,
  `paperclip-exec-creds`) have zero references to git/clone/worktree in their `src/`.
- **Conclusion**: an agent that spawns a coding harness (Claude Code / Codex / Gemini
  via the baked `acpx` ACP runtime — see §4) is free to `git clone` and
  `git worktree add` anywhere under its `workspace-{agent_id}/`, with no platform
  convention steering it to one location, and nothing that tells it (or reminds it) to
  ever run `git worktree remove` or delete a stale clone. This is exactly how Forge's
  workspace accreted "one git clone + 13 worktrees, 114k entries."

---

## 4. The Forge agent

**Forge is not a hardcoded/specially-provisioned agent anywhere in the codebase.**
`grep -rn "\bForge\b" apps/backend apps/infra` (excluding "salesforce"/"workforce"/
"reinforce"/"enforce" false positives) returns only illustrative-example usages:
- `core/containers/config.py:2079` — "company: Atlas can't find a sibling (e.g. a
  user-made `"Forge"`)" — i.e. Forge is used as a *hypothetical example of an
  owner-named agent*, not a real seed.
- `core/containers/config.py:2965` — "ACP coding-harness runtime (Forge-style
  delegation: a native agent can spawn Claude Code/Codex/etc.)" — again illustrative.
- `apps/infra/openclaw/Dockerfile:651` — "acpx is the official ACP runtime plugin that
  lets a native OpenClaw agent (**e.g. Forge**) spawn external coding harnesses — Claude
  Code, Codex, Gemini — over the Agent Client Protocol via
  `sessions_spawn({runtime:"acp"})`."
- Test fixtures name an agent `"Forge"` purely as flavor
  (`tests/unit/services/test_paperclip_agent_idempotency.py`,
  `test_paperclip_agent_lifecycle_meta.py`).

There is **no code path that treats "Forge" as a distinct identity**, and
`grep -rn "Isol8Admin" apps/backend apps/infra .github` returns **zero matches** in the
codebase — the GitHub identity CLAUDE.md documents (`Isol8Admin`, org admin, able to
`workflow_dispatch` its own prod deploy or regenerate `deploy.yml` for infra PRs) is
purely an **operational GitHub account / PAT**, configured outside this repo (a
per-owner integration credential, materialized the same way any GitHub credential
would be — not tracked in source).

Putting the evidence together: **Forge is an ordinary owner-created OpenClaw agent**
(most likely this org's own internal engineering-type agent, running in this org's
own workspace, one of the "e.g. Forge" acpx examples above) that has the `acpx`
ACP-runtime plugin enabled and a GitHub credential wired in, and uses both to spawn
Claude Code / Codex coding-harness sessions against real repositories (this very
repo, per the git-worktree-branch naming convention visible in this session's own
`git status` — `worktree-agent-*` branches). No routine/cron in
`agent_routine_provisioning.py` or elsewhere is Forge-specific or creates checkouts on
a schedule; the checkouts come from whatever coding tasks the agent runs interactively
or via Paperclip heartbeat wakes (§3), each of which is free to `git clone`/
`git worktree add` with no cleanup contract.

---

## 5. Container size, limits, monitoring

- **Standard box**: `PER_USER_CPU = "1024"`, `PER_USER_MEMORY_MIB = "4096"`
  (`ecs_manager.py:55-56`) — 1 vCPU / 4 GB.
- **Ultra box**: `ULTRA_CPU = "2048"`, `ULTRA_MEMORY_MIB = "8192"` (`ecs_manager.py:73-74`)
  — 2 vCPU / 8 GB.
- **Reliability floor** (not billing): `AGENT_RELIABILITY_RESIZE_THRESHOLD = 10`
  (`ecs_manager.py:77`) — `_size_for_owner` (522-620) upgrades ANY owner (regardless of
  plan) to the Ultra box once their **live Paperclip agent count** exceeds 10, reading
  the roster live rather than hooking every agent-creation path (comment 554-558:
  "cannot be bypassed by a creation path nobody remembered to instrument"). Triggered
  originally by an 11-agent owner crash-looping from 24 concurrent SQLite
  restores/replications starving a single vCPU (comment 547-552). `agent_count > 25`
  additionally emits a `container.large_agent_roster_observed` metric (601-610) as an
  operational tripwire, but does not change sizing further.
- **Ephemeral storage**: no override anywhere → Fargate default **20 GiB**, shared
  between `/tmp` scratch (browser/npx/uv, §1) and, if a redesign lands there, any
  task-local working-tree cache.
- **Disk usage monitoring**: EFS-level `PercentIOLimit` and `BurstCreditBalance` alarms
  exist in `apps/infra/lib/stacks/observability-stack.ts` (lines 2449-2477, 3741-3761)
  — these watch **EFS throughput/burst budget**, not per-owner byte/entry counts, and
  not the Fargate task's own 20 GiB ephemeral pool. **There is no per-owner disk-usage
  metric, no entry-count cap, and no cleanup cron for `workspace-{agent}/` trees
  anywhere in the backend or infra** — confirmed by grep across
  `apps/backend/core`, `apps/backend/routers`, and the CDK stacks for
  disk-usage/`df`/storage-bytes patterns (only hit: the two EFS IO alarms above).
  The only size caps that exist at all are marketplace-export-scoped
  (`_MAX_EXPORT_FILES`/`_MAX_EXPORT_BYTES` in `agent_export.py`, §2) — they bound what
  ships in a *published listing*, not what an agent may accumulate in its own
  workspace day to day.

---

## 6. Coupling map — what breaks if the working tree stops being EFS-readable from the backend

The backend's gateway WebSocket RPC surface today is narrow — a full grep of every
`send_rpc(...)`/`gateway_rpc_oneshot(...)` call site in `apps/backend/core/gateway/`,
`apps/backend/routers/`, and `apps/backend/core/services/` turns up exactly **four**
RPC methods ever invoked from the backend: `"agents.list"`, `"sessions.list"`,
`"sessions.describe"`, and an unrelated `"key"` lookup. The baked `lightbulb-channel`
plugin exposes exactly **one** HTTP route per configured agent account,
`POST /plugins/lightbulb-channel/inbound/<agentId>` (messaging ingress only —
`plugins/lightbulb-channel/src/channel.ts:1010`). **There is no existing RPC or plugin
route for reading, listing, or writing arbitrary files in an agent's workspace** — every
feature below reaches the workspace tree by mounting the EFS filesystem directly.

| Backend feature | Depends on direct EFS tree access? | Container-side RPC that could replace it today? |
|---|---|---|
| Workspace file viewer (`/workspace/{agent}/tree`, `/file`, `/config-files`) | **Yes** — full recursive tree walk (`_collect_recursive`, depth 10, unbounded entry count) | **None.** No `files.list`/`files.read` RPC exists over the gateway WS or any plugin route. Would need to be built (either a new OpenClaw RPC method, or an index the agent maintains and the backend queries). |
| Memory-wiki viewer (`/brain/tree`, `/brain/file`) | Yes, same shape, smaller subtree (`wiki/main/`, written by the container's memory-wiki plugin) | None — same gap, lower blast radius since the vault is small and plugin-managed rather than agent-grown. |
| File upload into agent workspace (`POST /files`) | Yes — `workspace.write_bytes` direct write | None — would need a container-side "accept a file at path X" RPC/route; today the backend writing straight to the mount **is** the delivery mechanism. |
| Marketplace/company publish (`agent_export.export_agent_from_efs`, `export_built_agent_from_staging`) | Yes — full `tarfile.add()` walk of `workspace-{agent}/`, run in `run_in_thread` | None — would need the container itself to produce the tarball (it already has the working tree) and hand it back over an RPC/HTTP route; the backend currently does this work itself specifically *because* it can reach EFS. |
| Marketplace/catalog deploy (`catalog_service.deploy_from_artifact`, `extract_tarball_to_workspace`) | Yes — backend untars directly into the buyer's `workspace-{new_agent}/` | None — same gap in reverse; would need a container-side "accept and unpack this tarball" RPC. |
| Company-agent materialization (`company_agent_materializer.materialize_agent`) | Yes — `write_file_if_missing`/`write_bytes` for `AGENTS.md` + skill assets | None. |
| Config patching (`config_patcher.py`, all mutators) | Yes, but scoped to `openclaw.json` only — **not** the workspace tree | Partially replaceable in principle: OpenClaw already hot-reloads `openclaw.json` via chokidar, so this is the one feature that *doesn't* strictly need direct backend file access if a "patch my own config" RPC existed — but none does today either. |
| Provisioning (`write_user_configs`) | Yes, but only three fixed files (`openclaw.json`, two `devices/` files) — never the workspace tree | N/A — doesn't touch workspace content. |
| `owner_facts.write_owner_facts` | Yes, single file, fully derived from DynamoDB | N/A — could trivially become an RPC push instead of a file write, since it's regenerated from DB state anyway. |
| Message image attachments (`store_image_attachment`/`load_image_attachment`) | Yes, but bounded to `media/messaging/attachments/` — small, capped files | Would need a small "fetch this attachment" RPC if EFS access were removed; low complexity given the bound. |
| Legacy auth-profile-row clearing (`clear_default_agent_auth_profile_row`) | **Already partially broken today** under the local-sqlite layout — the backend's EFS view of `agents/{id}/agent/openclaw-agent.sqlite` is stale/absent for any container that's gone through the SQLite migration, so the code falls back to a **sentinel file + required container boot** to apply the change (see §2). This is the working precedent for "backend can't reach live container-local state; use a sentinel + reboot/consume protocol instead." | The sentinel-and-reboot pattern itself, generalized. |

**Bottom line for the redesign**: moving `workspace-{agent}/` off EFS onto task-local
disk (with sync-back) would break *every* backend feature in the table above except
config-patching and owner-facts, because none of them has a container-side RPC
substitute today. The nearest existing analog inside this codebase for "how do you
make task-local state visible/durable without a live backend↔container RPC" is the
local-sqlite litestream design (§1): continuous incremental replication to S3, restore
on boot, tombstone-on-delete, fail-closed filesystem-type assertion — not a
request/response RPC. The nearest analog for "what's actually part of the agent vs.
disposable scratch" is `agent_export.py`'s nested-`.git` pruning + `.run/`
scratch-dir convention (§2/§3) — except `.run/` is documented as **durable**, so it
solves export cleanliness, not disk-growth bounding.

**Separately-open bug, same root cause, found live in this checkout**: the exact
incident this task is framed around (`GET /container/workspace/{agent}/tree?recursive=true`
walking Forge's 114k-entry workspace synchronously on the event loop, freezing the
whole backend for ~150s every ~6 minutes) is **still present in this branch** —
`routers/workspace_files.py`'s `list_workspace_tree`/`_collect_recursive` have no
`asyncio.to_thread` wrapper and no entry cap. Per this session's own memory
(`prod-backend-freeze-efs-tree-walk-2026-09-12`), a fix (PR #2563: wrap the walk in
`asyncio.to_thread`, add `MAX_TREE_ENTRIES = 5000`, surface `truncated` in the
response) was drafted for this exact incident, but `grep -rn "MAX_TREE_ENTRIES"
apps/backend/` returns zero hits in this checkout — **the fix has not landed on this
branch/main as of this research**. `routers/brain.py`'s `list_brain_tree`/
`read_brain_file` have the identical gap (§2), just against a smaller subtree.
