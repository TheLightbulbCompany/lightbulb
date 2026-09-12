# Agent working trees: repos and toolchain output leave EFS

Date: 2026-09-12. Status: design for owner review, not implemented. Sub-project 1 of two; sub-project 2 (the residual workspace tier and any container file RPC) belongs to the stateless-backend stream. Research reports are in `2026-09-12-agent-working-trees-research/` next to this file; delete both when this ships.

## 1. Context and incident

On 2026-09-12 `GET /container/workspace/{agent}/tree?recursive=true` walked Forge's
workspace on EFS synchronously on the event loop: 114,448 entries (already excluding
`node_modules`, `.git` and `__pycache__`), about 150 s per walk. The prod backend and
`/health` froze and ECS replaced the task roughly every 6 minutes.

EFS was under 4% of its I/O limit with full burst credits. Cost is latency times entry
count: 114k entries at EFS's documented ~1 ms read latency is about 114 s of serialized NFS
round trips (B §0), and that latency is identical in Bursting, Provisioned and Elastic modes
(B §3.1), so no EFS setting would have helped. PR #2563 fixed the route: one directory, off
the loop, expand on demand, recursive mode deleted. That stops the backend walking, not the
tree existing.

The tree existed because Forge's workspace held one clone, 13 per-issue git worktrees and
`paperclip-fork`. Nothing told it where to clone or when to clean up: zero matches for
`worktree`, `git clone` or `gh repo clone` in any bundled skill, plugin or seed template
(C §3), and there is no per-owner disk metric, entry cap or cleanup cron (C §5). The
content is also wrong for EFS: this repo has 3,518 tracked entries, but a checkout reaches
about 16.5k from ignored toolchain output (`apps/backend/.venv` ~14.9k,
`apps/frontend/.next` ~4.8k), with `node_modules` and `apps/infra/cdk.out` (~209k) on top.
Per-checkout cost is toolchain artifacts, not source, so sparse checkout barely helps
(D §4). No surveyed agent runtime puts a working tree on shared network storage (A §0), and
Anthropic states that Claude Code "never resumes into a worktree at a network path" (A §1b).

## 2. Goals and non-goals

Goals: no git repository and no toolchain output directory under `workspace-{agent}/` on
EFS; unpushed commits survive a container restart (15 commits on `forge/*` were never
pushed); bounded growth via a hard worktree cap plus a sweep that needs no human; parallel
tasks keep working, one worktree per active task; no new backend feature, no new AWS
service, no new IAM surface.

Non-goals: moving the authored workspace off EFS (R1); any backend RPC for container files
(C §6, stateless-backend stream); changing the SQLite or litestream design (C §1); a general
file-sync engine, since there is no litestream for a directory (B §4.4).

## 3. Options considered

| # | Option | EFS entries | Unpushed commits survive restart | git op latency | Cost delta | Verdict |
|---|---|---|---|---|---|---|
| a | Status quo plus a sweep on EFS | 5 to 8 checkouts, 40k+ | Yes | NFS, ~1 ms/op | 0 | Bounds growth, does not reduce it |
| b | **Worktrees on `/scratch`, bare repo on EFS** | Bare repo only, a few hundred | Yes | Mixed, see R2 | 0 | **Recommended** |
| c | Everything on `/scratch`, git remote the only store | 0 | No | Local | 0 | Loses work on every restart |
| d | Whole workspace ephemeral plus S3 archive | 0 | To last checkpoint | Local | +$46 per 100 owners/mo | Breaks every backend file feature |
| e | S3 Files or ECS-managed EBS | varies | varies | Unproven or unchanged | +$100 to +$1,600 per 1,000 | No proven latency win, or zero durability |

**(a) loses** because each worktree is a full working copy; only `.git` is shared (A §10.4,
D §2), so five worktrees pay 5x the metadata cost of one on every `git status`, install and
walk (D §5b). pnpm's store dedupes bytes, not entries: "the directory structure is
recreated for each project", one NFS round trip per entry (D §4). AWS advises under 10,000
directories per filesystem and warns `ls` over 100k files "can hang, not returning a
response" (B §3.5); Modal says network volumes "perform best when they contain less than
50,000 files and directories" and that latency "scales linearly with the number of files"
(A §7).

**(c) loses** on durability. It matches Codex, Copilot, Cursor, Devin and Jules, which all
destroy the checkout at task end (D §1), but it drops the 15 unpushed commits on every
deploy, OOM kill, Spot interruption and health restart. Codespaces deletes "irrespective of
whether a codespace contains unpushed changes" (A §3): fine for a human, not for an agent.

**(d) loses** on blast radius, not theory; it is what B §9 recommends in the abstract. It
breaks file viewer, brain viewer, upload, marketplace export, catalog deploy, company-agent
materialization and message attachments, none of which has a container-side RPC substitute
today (C §6). It also recreates the `clear_default_agent_auth_profile_row` bug class (the
backend edits a copy the running task never reads), which caused a founder-org prod
incident (C §2).

**(e) loses** twice. ECS-managed EBS on a service task is "always deleted upon task
termination" (B §2.1): cost and attach time for zero durability, and on a 1 vCPU box it
shares the I/O budget with ephemeral storage rather than adding to it (B §2.3). S3 Files is
a real sub-project 2 candidate on cost (B §8) but publishes no per-op latency, has no
`nconnect`, and imports metadata per directory on first access (B §4.3). Mountpoint for S3
is disqualified: no rename, no in-place modify, no file locking (B §4.2).

## 4. Design

### 4.1 Layout

```
/home/node/.openclaw/                    EFS, per-owner access point (durable)
  openclaw.json  .bak  .lock  .isol8-guard  .isol8-protocol
  workspace-<agentId>/                   authored content ONLY
    IDENTITY.md SOUL.md AGENTS.md MEMORY.md ...
    skills/  uploads/  .run/  media/
    (no .git, no node_modules, no .venv, no .next, no cdk.out, no target)
  repos/<repo>.git/                      BARE clone, packed objects, few entries
  state/                                 ECS-managed ephemeral volume (shadows EFS)
  agents/                                ECS-managed ephemeral volume (shadows EFS)

/scratch/                                plain directory on Fargate ephemeral storage,
                                         wiped on every task stop (same tier as /tmp/openclaw-*)
  wt/<repo>/<task>/                      git worktree add, one per active task
  cache/
    pnpm-store/                          PNPM store dir
    uv/                                  UV_CACHE_DIR
  state.json                             task -> {repo, branch, path, created}
```

| Rule | Enforced by |
|---|---|
| No `.git` under `workspace-<agentId>/` | helper refuses; boot detector emits a metric |
| No toolchain dir under `workspace-<agentId>/`, working trees only under `/scratch/wt/` | the helper is the only creator |
| N worktrees cost about one install in bytes | shared `PNPM_HOME` store-dir and `UV_CACHE_DIR` |
| The bare repo is the durable object store | `git worktree add` from it, so commits land there at once |

The export code already prunes any subtree containing a nested `.git` as "not part of the
agent's portable definition" (C §2b, `agent_export.py:591-599`); this promotes that filter
to a rule. `/scratch` is the tier the container already uses for `/tmp/openclaw-browser`,
`/tmp/openclaw-npx` and `/tmp/openclaw-uv`, moved off EFS because chromium fsync traffic
and thousand-file npx/uv unpacks were triggering NFS RPC stalls (C §1). Same precedent,
one directory over.

### 4.2 Lifecycle

| State | Entered by | Leaves by |
|---|---|---|
| `absent` | default | `lightbulb-checkout <url> <branch>` |
| `active` | worktree created under `/scratch/wt/<repo>/<task>` | agent commits, which land in the EFS bare |
| `pushed` | `git push` sets an upstream on origin | PR merged, or ages out |
| `merged` | sweep sees `MERGED` from `gh pr view`, or `git branch --merged` | `git worktree remove`, branch deleted |
| `orphan-record` | restart wiped `/scratch`, EFS admin record survived | `git worktree prune` at boot |
| `held` / `flagged` | dirty, stashed, locked or unpushed with no upstream / unmerged past `LIGHTBULB_WT_MAX_AGE_DAYS` (default 7) | the agent finishes it / a human |

Hard cap: 6 per agent. At the cap the helper sweeps once, retries once, then refuses with
exit 3 and prints why each worktree is held. That turns unbounded growth into a bounded
queue, which is the actual fix; a sweep alone only slows growth if task volume stays
behind merge rate (D §3).

Sweep algorithm: D §3 verbatim. Skip locked, skip dirty (`git status --porcelain`
non-empty), skip stashed, push first when an upstream exists, skip if commits remain
unpushed after the push, remove when merged, flag stale unmerged past N days,
`git worktree prune --expire 1.day` as a backstop. Runs at boot and on a PR-merged signal
for a branch this agent owns, not on every push. Lost on restart: the uncommitted diff,
nothing else, since commits are already in the EFS bare. Acceptable because every surveyed
vendor treats the checkout as derivable rather than durable (A §0, A "What to copy" 2), and
because checkouts on EFS caused the incident. What makes it safe is discipline, not storage:
commit before any interruptible step.

**The known cost of this shape.** Worktrees under `/scratch` share the EFS bare repo's
object store, refs and per-worktree index (`<bare>/worktrees/<name>/index`), so working-tree
reads and writes (the bulk of the entries and of the incident) become local while git's own
object and index I/O stays on NFS. Deliberate trade: commits are durable the instant they
are made, at the cost of NFS-latency git metadata. Unmeasured here, and checkout on NFS has
been measured elsewhere at 50x to 130x slower than local SSD for a 70k-file repo (D §2,
Tavares). §6 benchmarks it; R2 carries the alternative.

### 4.3 The helper

`lightbulb-checkout`, a shell script baked into the extended image at `/usr/local/bin/`,
the only thing that creates or removes a working tree.

| Command | Does | Exit |
|---|---|---|
| `lightbulb-checkout <repo-url> <branch> [--task <id>] [--sparse <dir>...]` | Ensures `~/.openclaw/repos/<repo>.git` exists (`git clone --bare` first time, `git fetch` after), then `git worktree add /scratch/wt/<repo>/<task> -b <branch>`. Prints the path. | 0 ok, 3 cap reached, 4 disk low, 5 fetch failed |
| `--sweep` | D §3 across every worktree of every repo. Prints a JSON summary. | 0 always, non-zero only if `git` itself fails |
| `--prune` | `git worktree prune` on every bare repo. Local metadata only, fast. | 0 |
| `--list` | JSON of live worktrees with state and hold reason. | 0 |
| `--release <task>` | Push if an upstream exists, then remove if clean. | 0 removed, 2 kept (dirty or unpushed) |

Exit behaviour is the contract: stdout is the path or JSON, stderr is the reason, a
non-zero exit names what to do next, and the helper never deletes work to make room.
`--sparse` is low leverage, since the dominant entry cost is toolchain output rather than
source (D §4); it exists only because cone mode is free and stores config per-worktree
since Git 2.36 (D §2.1). The sweep also runs `git maintenance` on each bare repo: loose
objects on EFS are themselves thousands of small-file entries, the Jenkins failure mode
(D §2.5, A §10.2). Do not enable `core.fsmonitor`: its daemon needs a Unix-domain socket
plain NFS does not support, and it is experimental on network filesystems even with
`fsmonitor.allowRemote=true` (D §2.7).

### 4.4 Boot sequence

Existing chain (C §1): lock recovery, symlink migrations, Xvnc, chown the two ECS-managed
volumes as root, then `exec setpriv --reuid=1000 ... isol8-local-sqlite-boot.sh`, which
restores SQLite from S3 and execs litestream with `isol8-local-sqlite-app.sh`, which runs
`openclaw doctor --fix` (52 to 84 s measured) and then the gateway. Inserted steps:

1. As root, before `setpriv`: `mkdir -p /scratch/wt /scratch/cache`, chown 1000:1000. Same
   reason the existing volumes need it: ECS materializes them root:root (C §1).
2. In `isol8-local-sqlite-app.sh`, before `doctor --fix`: `lightbulb-checkout --prune`. It
   must run before anything can `git worktree add`, or a same-named task collides with a
   dangling record (D §5a). The nested-`.git` detector runs here too.
3. Immediately before the gateway `exec`, backgrounded:
   `lightbulb-checkout --sweep >> /scratch/sweep.log 2>&1 &`. It calls `gh pr view` per
   branch and is network-bound, and the 300 s health-check `startPeriod` is already sized
   around EFS first touch, `doctor --fix` and the cold MCP connect storm (C §1).

### 4.5 Error handling

| Condition | Behaviour |
|---|---|
| `/scratch` above 85% | Refuse with exit 4 after running `--sweep`, print `df` and the held list. Never auto-delete unpushed work to free space. |
| Push fails (no network, no creds, rejected) | Worktree kept, marked `held`, sweep skips it until pushed. Reported in the summary and the metric. |
| Cap reached | Sweep once, retry once, exit 3 with per-worktree hold reasons. |
| Restart mid-task | `/scratch` gone, boot prune clears records, commits are in the EFS bare, uncommitted diff lost. |
| Bare repo corrupt or fetch fails | Exit 5. Never silently re-clone over a repo that may hold unpushed commits. |
| Two tasks want one branch | Exit 3 with the existing path; `git worktree add` refuses it natively. |

### 4.6 Observability

One CloudWatch EMF JSON line per boot and per sweep, to stdout. The per-user task already
logs to the `openclaw` log group via the awslogs driver and the backend already emits
metrics this way (`core/observability/metrics.py`), so EMF needs no new IAM, no metric
filter and no backend code. Namespace `LightbulbAI/Containers`, dimension `owner_id`:

| Metric | Alarm |
|---|---|
| `scratch.used_percent` | 80% of ephemeral storage |
| `scratch.worktrees`, `scratch.worktrees_held` | dashboard only |
| `workspace.entries` | above 5,000 |
| `workspace.nested_git_dirs` | above 0 |

Set `ephemeralStorage.sizeInGiB` to 40 up front: 6 worktrees with `node_modules`, `.venv`,
`.next` and `cdk.out`, plus the image and `/tmp/openclaw-*`, plausibly exceed 20 GiB on day
one, and the extra 20 GiB costs $1.62 per container-month against about $42 of compute
(B §1). Raise further only on evidence from `scratch.used_percent`.

## 5. What changes where (touches only, no code)

| Component | Path | Touch |
|---|---|---|
| Task definition | `apps/infra/lib/stacks/container-stack.ts` | `ephemeralStorage.sizeInGiB: 40`; `/scratch` mkdir plus chown in the boot command; env `LIGHTBULB_SCRATCH`, `UV_CACHE_DIR`, `PNPM_HOME`, pnpm store-dir, `LIGHTBULB_WT_CAP`, `LIGHTBULB_WT_MAX_AGE_DAYS` |
| Helper and image | `apps/infra/openclaw/scripts/lightbulb-checkout` (new), `apps/infra/openclaw/Dockerfile` | the script itself, baked to `/usr/local/bin/` |
| Boot chain | `apps/infra/openclaw/local-sqlite/isol8-local-sqlite-app.sh` | `--prune` before `doctor --fix`; backgrounded `--sweep` before the gateway exec |
| Skill | `apps/infra/openclaw/skills/lightbulb/references/working-trees.md` (new) | where to check out, the cap, the sweep, commit-before-interruptible |
| Skill routing | `.../skills/lightbulb/SKILL.md` and `MAINTENANCE.md` | one routing-table row; new anchor Working trees -> references/working-trees.md <- helper, container-stack.ts env |
| Seed | `apps/backend/core/services/starter_roster/files/engineering/AGENTS.md` | one line pointing at the helper |
| Backend | `core/services/agent_export.py`, `routers/brain.py`, `core/containers/workspace.py` | export caps already exist (R5); `list_brain_tree` walks in a thread since PR #2563, `read_brain_file` and `cleanup_agent_dirs` still do sync EFS I/O on the loop |
| Observability | `apps/infra/lib/stacks/observability-stack.ts` | alarm on `scratch.used_percent`, dashboard widgets |
| Version pin and docs | `openclaw-version.json`, `CLAUDE.md` | `dev.tag` then `prod.tag`; the `/scratch` tier in the data-flow diagram |

## 6. Testing and verification

Measure on Forge's container, before and after, same repo and branch.

| Measurement | Command shape | Before | Target |
|---|---|---|---|
| Workspace entry count | `find workspace-<agent> \| wc -l` | 114,448 | under 2,000 |
| Files root listing | time `GET /container/workspace/<agent>/tree` (level 1) | ~150 s recursive | under 500 ms |
| `git status` in a worktree | `time git status`, EFS vs `/scratch` | unmeasured | record both, gates R2 |
| Fresh install | `time pnpm install --frozen-lockfile`, `time uv sync`, EFS vs `/scratch`, cold and warm cache | unmeasured | record all four |
| Peak scratch usage | `df /scratch` with 6 worktrees | unmeasured | under 80% of 20 GiB |

B §10 unknowns to settle:

1. **Ephemeral storage backing** (B §10.6). The EBS-on-Fargate page says the limit "is
   shared between the task's ephemeral storage and the attached volume", implying EBS-class
   network block storage, not the instance-store NVMe `CLAUDE.md` claims. Moves the expected
   numbers, not the conclusion.
2. **Fargate Spot plus volumes** (B §10.2). No AWS statement either way, and per-user tasks
   run on Spot with on-demand fallback. Verify before the fleet roll.
3. **Hard-link need for `git worktree`** (B §10.7) and **S3 Files per-op latency on a
   114k-entry walk** (B §10.5). Sub-project 2 only.

## 7. Rollout

Order matters. `container-stack.ts` hard-`exit 1`s when a boot-script path is missing, so a
CDK deploy landing before the image fails every container boot.

1. Land the helper, boot hook and skill reference. `build-openclaw-image.yml` pushes to ECR.
2. Land the `container-stack.ts` env, `/scratch` and 40 GiB change; run `cd apps/infra && npx cdk
   synth` and commit the regenerated `deploy.yml` in the same PR.
3. Flip `openclaw-version.json` `dev.tag`, reprovision one dev owner, run §6.
4. Validate on the founder container in prod first (it has the known-bad workspace and the
   incident's numbers), then flip `prod.tag` and roll the fleet. Prod ships on
   `workflow_dispatch`, not on merge.
5. One-time cleanup per owner, after the image is live: seed `~/.openclaw/repos/<repo>.git`
   from the existing clone so the 15 unpushed commits move into the bare repo *before*
   anything is deleted, then remove the main clone and `paperclip-fork` (the 13 `forge/*`
   worktrees were removed on 2026-09-12; the main clone was kept precisely because it holds
   those 15 unpushed commits). For other owners run the detector first
   (`workspace.nested_git_dirs`) and clean only what it flags. Never sweep blind.

## 8. Open questions, with provisional rulings (each made without the owner)

**R1. Should the residual authored workspace ever leave EFS?**
Ruling, provisional: not now. After sub-project 1 it is small and low-fanout, EFS handles
it, and every backend file feature keeps working without new container RPCs (C §6). Revisit
only for cost, benchmark-gated, per B §4.3 and B §9. Sub-project 2 belongs to the
stateless-backend stream.

**R2. Where do per-task checkouts live, and what is the durable object store?**
Ruling, provisional: `/scratch` for working trees, a bare clone on EFS at
`~/.openclaw/repos/<repo>.git` for objects and refs, nothing under `workspace-{agent}/`
containing a `.git` or a toolchain directory. `/scratch` is a plain `mkdir` on the Fargate
ephemeral pool, like the existing `/tmp/openclaw-*` dirs (C §1); a named ECS volume would buy
only a separate filesystem for `df`. Open: (ii)
Bare-on-EFS, or a `/scratch` clone that pushes to the EFS bare? As written it keeps git's
object and index I/O on NFS (§4.2). Gate on the §6 `git status` number.

**R3. What is the worktree lifecycle?**
Ruling, provisional: one per active task, hard cap 6, `git worktree prune` unconditionally
at boot, sweep at boot and on PR-merged using D §3 verbatim, commit and push before any
interruptible step, uncommitted diff lost on restart. Open: whether 6 fits a 20 GiB pool
shared with the image and `/tmp`. §6 measures it.

**R4. How is this enforced?**
Ruling, provisional: a container-side helper, a skill reference, a line in the seeded
`AGENTS.md`, env vars in the task definition, no new backend feature. Open: nothing stops
an agent that ignores the skill from cloning into the workspace. The §4.6 detector makes
that visible within one boot; whether visibility is enough, or the sweep should relocate a
nested repo, is undecided.

**R5. What may the backend do to a working tree?**
Ruling, provisional: never enumerate one. Level-by-level listing is the only shape, and
`agent_export`/`catalog_service` stay thread-offloaded and bounded by the now-small residual
workspace. The export entry cap already exists: `agent_export.py` carries
`_MAX_EXPORT_FILES = 2000` and `_MAX_EXPORT_BYTES = 50 MiB`, raising `ExportTooLargeError`
mid-walk (C §2). `list_brain_tree` walks in a thread since PR #2563. The remaining sync EFS
I/O on the event loop is bounded: `read_brain_file`, `read_workspace_file`, and
`workspace.cleanup_agent_dirs`'s `rm -rf` walk (C §2); a separate small PR moves them to
threads.

**R6. What is measured, and how much disk?**
Ruling, provisional: per-container `/scratch` usage and worktree count in the existing
metrics path, alarm at 80% of ephemeral storage, and `ephemeralStorage.sizeInGiB: 40` from
the start ($1.62 per container-month, B §1), because "evidence" for a later bump would mean
an agent hitting disk-full mid-task. Raise above 40 only on the `scratch.used_percent`
number.

**R7. What does the owner lose?**
Ruling, provisional: checkouts on `/scratch` are invisible to the web Files panel; code
review happens on GitHub. No surveyed runtime's control plane enumerates the agent's
working tree (A "What to copy", bonus).

## 9. Out of scope

- Any backend RPC to read, list or write container files (C §6, stateless-backend stream);
  moving `workspace-{agent}/`, `media/`, `skills/` or `cron/` off EFS (R1).
- S3 Files, ECS-managed EBS, AgentCore, Firecracker (§3e, B §5, B §6); changing the SQLite
  or litestream design (C §1).
- The `.run/` convention: marketplace-export cleanliness, documented as durable, so it does
  not bound disk (C §3). The remaining bounded sync EFS reads on the event loop (R5):
  separate small PR.

## Sources

- https://code.claude.com/docs/en/worktrees
- https://docs.aws.amazon.com/efs/latest/ug/performance.html
- https://docs.aws.amazon.com/efs/latest/ug/performance-tips.html
- https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-storage.html
- https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ebs-volumes.html
- https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ebs-fargate-performance-limits.html
- https://aws.amazon.com/fargate/pricing/
- https://docs.aws.amazon.com/AmazonS3/latest/userguide/mountpoint.html
- https://modal.com/docs/guide/volumes
- https://git-scm.com/docs/git-worktree
- https://pnpm.io/symlinked-node-modules-structure
- https://matheustavares.dev/posts/parallel-checkout
- https://dev.to/tielec-takashi/how-git-temp-files-killed-our-jenkins-performance-efs-metadata-iops-hell-3ff4
- https://docs.github.com/en/codespaces/about-codespaces/deep-dive
- https://cognition.ai/blog/blockdiff
- https://developers.openai.com/codex/cloud

Research reports (full evidence, with per-claim sources): `2026-09-12-agent-working-trees-research/A-remote-runtimes.md`, `B-aws-storage.md`, `C-our-storage-layout.md`, `D-worktree-hygiene.md`.
