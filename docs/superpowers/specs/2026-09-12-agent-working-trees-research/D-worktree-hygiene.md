# Worktree hygiene for a per-customer OpenClaw fleet — research

Context recap: each customer's OpenClaw container is 1 vCPU/4GB Fargate, workspace on EFS
(NFS, ~1ms/metadata-op). Forge creates one `git worktree` per issue (`forge/<issue>`) and
never removes them. 13 worktrees + main clone of a repo this size = 114k directory entries,
150s for a backend recursive listing.

**Grounding check on this repo:** run against the actual Lightbulb monorepo (this checkout),
`git ls-files | wc -l` = **3,003 tracked files / 476 directories**; `apps/frontend` alone is
1,143 files, `apps/backend` 996, `packages/shared` 51. `git worktree list` in this very repo
already shows **11 live worktrees** (`.claude/worktrees/*`, `.worktrees/*`, a stray
`isol8.nosync/.worktrees/*`, a stray `lb-wt.nosync/*`) — i.e., the hygiene problem this
research is about is already reproduced in-repo right now. Since tracked source alone is 3k
files, not 16k, the 16k/114k figures in the prompt almost certainly count **`node_modules`**
per checkout, not tracked source — see §4, this is the load-bearing finding of this report.

---

## 1. How coding-agent products isolate per-task checkouts, and their lifecycle

| Product | Isolation unit | Where checkouts live | Lifecycle |
|---|---|---|---|
| **Claude Code** `--worktree`/`-w` | `git worktree` under `.claude/worktrees/<name>/`, new branch `worktree-<name>` | Inside the repo (recommends `.gitignore`-ing `.claude/worktrees/`) | Interactive unnamed session: auto-removed (worktree + branch) on exit **if clean** (no changed/untracked files, no new commits). Named session: prompts first. `-p` (headless) runs leave the worktree + a `git worktree lock` in place — nothing auto-cleans them until a later session's periodic stale-lock sweep. |
| **Claude Code** subagent `isolation: "worktree"` | Temporary git worktree per subagent dispatch, branched from the base ref | Same `.claude/worktrees/` tree | Auto-removed when the subagent finishes **unchanged**; if it left changes, kept on disk until the periodic sweep (gated on `cleanupPeriodDays` setting) can remove it *without losing work* — the sweep explicitly skips any worktree with uncommitted/untracked files or unpushed commits, or one that's actively locked (agent still running). |
| **Claude Code** `EnterWorktree`/`ExitWorktree` | Mid-session tool call, same `.claude/worktrees/` convention | Same | Manual; leaving one via `ExitWorktree` doesn't delete it — "the worktree you left stays on disk untouched." |
| **OpenAI Codex Cloud** | Fresh container per task, repo checked out fresh each time | Ephemeral cloud container, not attached to any persistent disk | Container is **disposable, cached at most 12 hours**, then gone regardless of task outcome. |
| **GitHub Copilot coding agent** | Fresh GitHub Actions runner + fresh clone per session | GitHub-hosted (or self-hosted via ARC) Actions runner, destroyed after the job | "Every task starts clean, ends clean" — each session gets its own container, destroyed after completion; no persistent state between sessions. |
| **Cursor Background/Cloud Agents** | Dedicated cloud VM per agent run, ephemeral checkout | Cursor's cloud infra | Spun up per background task; no evidence found of long-lived retained checkouts — treated as disposable compute, not a persistent workspace. |
| **Devin** | Fresh VM booted from a clean snapshot per session | Devin's cloud infra | VM destroyed when the task finishes; anything not pushed/exported is lost. |
| **Google Jules** | Fresh ephemeral Cloud VM per task, clones the repo (or restores an "Environment Snapshot" for faster setup) | Google-managed ephemeral VM | Destroyed after task completion; code doesn't persist. |
| **Aider** (community pattern, not a first-party feature) | `git worktree` per task under a scratch dir (e.g. `.trees/<task>/`), one agent invocation per worktree | Inside or beside the repo | No built-in lifecycle — cleanup is left to the user/wrapper script; commonly recommended "for ephemeral agents completing in under an hour," implying manual or scripted teardown, not automatic. |

**Pattern across all of them:** every cloud/CLI product that isn't Claude Code's local
`--worktree` flag treats the per-task checkout as **fully disposable compute**, destroyed
unconditionally at task end — nothing persists by default, and *pushing to the remote* is
the only way work survives. Claude Code is the outlier that keeps a *local, on-disk* worktree
because a human may want to inspect/resume it, but even there the default is delete-if-clean,
and the explicit design intent for the messy case (leftover uncommitted work) is "never
silently delete work — sweep on a timer, skip anything with unpushed/uncommitted state."
None of them accumulate: the common failure mode search turned up (Claude Code issues
[#57767](https://github.com/anthropics/claude-code/issues/57767) and
[#63088](https://github.com/anthropics/claude-code/issues/63088)) is specifically about the
auto-cleanup *hook not firing* when a subagent drops a scratch file — i.e., accumulation is
treated as a bug to fix, not a tolerated steady state.

Sources:
- [Claude Code docs — Run parallel sessions with worktrees](https://code.claude.com/docs/en/worktrees) (fetched in full; primary source for the whole Claude Code row)
- [anthropics/claude-code#57767 — worktree leftover visibility, Windows lock recovery](https://github.com/anthropics/claude-code/issues/57767)
- [anthropics/claude-code#63088 — subagent worktrees not auto-cleaned when reviewer writes scratch files](https://github.com/anthropics/claude-code/issues/63088)
- [OpenAI Codex Cloud — container lifecycle description](https://codex.danielvaughan.com/2026/04/21/cloud-development-environments-codex-cli-coder-daytona-infrastructure/) — "disposable (cached for at most 12 hours)"; ephemeral-agent-pool pattern
- [GitHub — About GitHub Copilot cloud agent](https://docs.github.com/copilot/concepts/agents/coding-agent/about-coding-agent)
- [GitHub Blog — Copilot coding agent 101](https://github.blog/ai-and-ml/github-copilot/github-copilot-coding-agent-101-getting-started-with-agentic-workflows-on-github/) — "Every task starts clean, ends clean"
- [GitHub Changelog — self-hosted runners for Copilot coding agent](https://github.blog/changelog/2025-10-28-copilot-coding-agent-now-supports-self-hosted-runners/)
- [Cursor Docs — Cloud Agents](https://cursor.com/docs/cloud-agent)
- [iamraghuveer.com — Cursor Background Agents: branch isolation, resource limits](https://www.iamraghuveer.com/posts/cursor-background-agents/)
- [Fastio — Devin AI Software Engineer Architecture, Sandboxes & CLI](https://fast.io/resources/devin-software-engineer/)
- [Fastio — Devin AI Sandbox File Sharing and Persistence](https://fast.io/resources/devin-ai-sandbox-file-sharing/)
- [digitalapplied.com — Google Jules async coding agent guide](https://www.digitalapplied.com/blog/google-jules-gemini-async-coding-agent-guide) — task pool / VM fleet / Gemini description
- [Google Labs — Jules](https://blog.google/innovation-and-ai/models-and-research/google-labs/jules/)
- [Augment Code — Git worktrees for parallel AI agent execution](https://www.augmentcode.com/guides/git-worktrees-parallel-ai-agent-execution) — "recommended when agents are ephemeral and tasks complete in under an hour"
- Unverified: I found no first-party Aider documentation describing a built-in worktree-per-task feature; every source describing Aider+worktrees was a third-party pattern/wrapper, not an Aider feature. Mark this as **unverified as an Aider-native behavior**.

---

## 2. Git techniques that cut entry count and NFS metadata cost

**Baseline fact governing everything below:** metadata ops (`lookup`, `getattr`, `setattr`,
`mkdir`, `rmdir`) are the expensive unit on NFS/EFS, not bytes. AWS's own guidance:
*"EFS is not suitable for workloads that have a random seek component... running Git or
other code repositories is not efficient because it requires very frequent access to a large
number of small files."* ([AWS re:Post](https://repost.aws/questions/QUm_ED7FMwRd6F2vXuihyuEw/why-do-ec2-and-ecs-instances-with-efs-mounted-perform-poorly-when-reading-and-writing-a-large-number-of-small-files-and-how-to-optimize)) A real incident write-up
([dev.to — Git temp files vs. EFS metadata IOPS](https://dev.to/tielec-takashi/how-git-temp-files-killed-our-jenkins-performance-efs-metadata-iops-hell-3ff4))
shows this compounding: leaked `tmp_pack_*` files on EFS drove metadata-IOPS to 100%
utilization for hours before anyone noticed, because nothing purged them. AWS's own perf
guide: [Amazon EFS performance specifications](https://docs.aws.amazon.com/efs/latest/ug/performance.html) — General Purpose mode has the lowest per-op latency; use NFS 4.1 and 1MB
client read/write buffers.

**git worktree already shares the object store — that part is not the problem.** Per the
[git-worktree docs](https://git-scm.com/docs/git-worktree): all worktrees share `refs/`, the
object database/packs, and config; only `HEAD`, the index, and a few worktree-scoped refs are
per-worktree (a handful of small files under the *main* repo's `.git/worktrees/<name>/`, not
duplicated per checkout). **So a linked worktree does not multiply `.git` internals** — the
114k-entry blow-up in the prompt is coming from the **working tree**, not from N copies of
`.git`. (If Forge were instead doing a full `git clone` per issue rather than
`git worktree add`, each clone would carry its own loose-object store too — worth confirming
which Forge is actually doing, since the fix differs.)

**Techniques, in order of leverage for this exact problem (NFS entry count, not disk bytes):**

1. **`git sparse-checkout set --cone <dirs>`** (cone mode, the default since Git 2.37) —
   materializes only the named directory prefixes; everything else collapses to nothing on
   disk. Cone mode also enables the **sparse index**, where excluded directories collapse to
   *one* index entry instead of one per file — git's own numbers: a 300k-file repo's index
   goes from 25MB to 300KB. ([git-scm.com/docs/git-sparse-checkout](https://git-scm.com/docs/git-sparse-checkout)) Since Git 2.36, sparse-checkout settings are **stored per-worktree** automatically
   (`extensions.worktreeConfig`), so each Forge worktree can have a *different* cone without
   fighting the others.
   - Concrete for this repo: full tracked tree = 3,003 files / 476 dirs. A worktree scoped to
     just `apps/backend/` (996 files) or `apps/frontend/` (1,143 files) + `packages/shared`
     (51) cuts materialized entries by 55-65% for a single-app fix, before touching
     `node_modules` at all.
2. **`--filter=blob:none` partial clone**, paired with sparse-checkout — the *history* fetch
   shrinks (no blobs for paths outside the cone), but this mainly saves clone/fetch bandwidth
   and the object store, not working-tree entry count, since worktrees already share the
   object store. Lower leverage here than sparse-checkout, but free to add if you also want
   faster first-clone/faster CI fetches. ([GitHub Blog — sparse-checkout for monorepos](https://github.blog/open-source/git/bring-your-monorepo-down-to-size-with-sparse-checkout/), [git-scm docs](https://git-scm.com/docs/git-sparse-checkout))
3. **`--depth`/shallow clone** — shrinks history transfer only; irrelevant to steady-state
   NFS entry count since history lives in the (already-shared) object store, not the working
   tree. Low leverage for this specific problem.
4. **Alternates/`--reference`** — redundant once you're already using `git worktree add`
   (worktrees get shared objects automatically via the internal alternates mechanism); only
   matters if checkouts are separate clones instead of worktrees.
5. **`git maintenance`** (`git maintenance start`, incremental strategy) — runs `prefetch` +
   `commit-graph` hourly, `loose-objects`/`incremental-repack` daily, `pack-refs` weekly.
   Keeps the *shared* object store from bloating (loose objects → packs), which matters
   because a bloated `.git/objects` on EFS is itself thousands of small-file entries — this is
   exactly the failure mode the Jenkins/EFS postmortem hit. ([git-scm.com/docs/git-maintenance](https://git-scm.com/docs/git-maintenance))
6. **`core.untrackedCache` + `core.preloadIndex`** (or just `feature.manyFiles=true`, which
   sets both plus `index.version=4`) — don't reduce entry count, but reduce the number of
   `lstat()` round-trips `git status`/`git add` make against those entries. Real win on NFS
   specifically because NFS's `lstat` latency (not local-FS caching) is the bottleneck;
   measured local improvement from untrackedCache alone was 2s→0.8s for `git status` on one
   large repo (that number is from a normal disk, so treat as directional, not an NFS
   number — **unverified for EFS specifically**).
7. **`core.fsmonitor`** — **not viable as configured today, and don't rely on it.** Git's
   native fsmonitor daemon uses a Unix-domain socket for IPC that plain NFS mounts don't
   support; it refuses network-mounted repos by default (override via
   `fsmonitor.allowRemote=true`) and is explicitly documented as "considered experimental" on
   network filesystems even then. If `$HOME` itself is on the network mount, the socket has
   to be redirected to a local path via `fsmonitor.socketDir`. ([git-fsmonitor--daemon(1)](https://git-scm.com/docs/git-fsmonitor--daemon)) Net: this is the one lever from the "known git+NFS knobs" list to actively avoid,
   not adopt.
8. **Vendor/community guidance on git-on-NFS in general:** GitLab has explicitly deprecated
   NFS for Gitaly (git repo storage) — "technical and engineering support for using NFS to
   store Git repository data is officially end-of-life," citing data loss and inconsistent
   post-push reads on high-traffic repos, and recommends against EFS/Filestore-class network
   filesystems entirely. ([GitLab NFS docs](https://gitlab.com/gitlab-org/gitlab/-/issues/29310), [GitLab 15.0 NFS EOL tracking issue](https://gitlab.com/gitlab-com/support/support-team-meta/-/issues/4032)) This is industry
   consensus, not a one-off complaint, and it argues for keeping the **live working checkout**
   off EFS/NFS wherever the platform allows an alternative (see §5).

Sources (additional, beyond inline links above):
- [Matheus Tavares — Faster Git checkouts on NFS and SSD with parallelism](https://matheustavares.dev/posts/parallel-checkout) — up to 50-130x slower checkout on NFS vs local SSD for a 70k-file repo; `checkout.workers <N>` mitigates via parallel I/O
- [Git users list — "git on nfs is outrageously slow"](https://groups.google.com/g/git-users/c/CHPB-5p2Yec)
- [Amazon EFS performance specifications](https://docs.aws.amazon.com/efs/latest/ug/performance.html)

---

## 3. Cleanup mechanics and a concrete sweep algorithm

**Primitives:**
- `git worktree remove <path>` — removes a *clean* worktree (no uncommitted/untracked
  changes) and its administrative metadata in one step. Refuses on dirty state or a locked
  worktree unless `--force` (dirty) / `--force --force` (locked) is passed twice.
- `git worktree prune [--expire <time>]` — only cleans up *administrative* records for
  worktrees whose directory was deleted out-of-band (e.g. `rm -rf` instead of `remove`); best
  practice is to always use `remove` so `prune` is never load-bearing. ([git-worktree docs](https://git-scm.com/docs/git-worktree))
- `git branch --merged <default-branch>` — lists branches whose tip is an ancestor of the
  default branch, i.e., safely merged.
- `gh pr view <branch> --json state,mergedAt,headRefName` — authoritative merge status from
  GitHub itself (more reliable than local `--merged` alone, since a squash-merge rewrites
  commits and `git branch --merged` can miss it; `gh pr list --state merged --json ...` also
  works for a bulk pass).
- Never-delete guards found repeatedly in tooling write-ups: skip a worktree that has
  uncommitted changes, untracked files, git stashes (`git stash list` scoped to that
  worktree — stashes are shared across worktrees via the main `.git`, so check by working
  directory, not by worktree), or commits not yet pushed to any remote-tracking branch
  (`git log @{u}.. ` / `git cherry -v origin/<branch>`). Community skills (e.g., "Prune
  Worktrees") independently converge on identical rules: require merged branch + no
  uncommitted changes + not the primary checkout + explicit confirmation. ([skills.rest — cleanup-git](https://skills.rest/skill/cleanup-git), [brtkwr.com — Bulk cleaning stale git worktrees](https://brtkwr.com/posts/2026-03-06-bulk-cleaning-stale-git-worktrees/))

**Proposed sweep (boot-time AND post-merge), safe-by-construction:**

```
for each worktree W in `git worktree list --porcelain` (excluding the main worktree):
    if W is locked (git worktree lock)                 -> skip   # active task
    if `git -C W status --porcelain` is non-empty      -> skip   # dirty: uncommitted/untracked
    if `git -C W stash list` is non-empty               -> skip   # has stashes
    branch = current branch of W
    push branch first if it has commits not on any remote-tracking ref:
        git -C W log @{u}.. --oneline   # non-empty -> `git -C W push` before continuing,
                                          # or skip if push fails (no assumption of network/creds)
    if push still leaves unpushed commits (no upstream configured, push failed) -> skip
    merged = (`git branch --merged <default>` includes branch)
             OR (`gh pr view <branch> --json state,mergedAt` reports state=MERGED)
    if not merged AND age(W) < max_age_days            -> skip   # still active, not yet reviewed
    if not merged AND age(W) >= max_age_days            -> flag for human (stale, unmerged, aging out)
    if merged:
        git worktree remove W
        git push origin --delete branch   # optional, if branch is fully local-owned by this bot
        git branch -D branch               # only after remove succeeds
git worktree prune --expire 1.day   # backstop for anything removed out-of-band
```

Run this (a) on container boot (catches everything that piled up while asleep/between
deploys) and (b) right after a PR merge webhook/poll fires for a branch this agent owns —
not on every commit, to avoid hammering `gh pr view` per-push. Cap total worktrees (e.g. hard
ceiling of ~5-8 concurrent) and refuse to create a new one past the ceiling until the sweep
frees a slot — this converts "unbounded growth" into "bounded queue," which is the actual
fix; the sweep alone only slows growth if task volume keeps ahead of merges.

---

## 4. Node/JS specifics — the likely real culprit behind "16k files / 114k entries"

- **`node_modules` is not shared by `git worktree`.** Worktrees only share git-internal state
  (§2); the *working tree*, including anything a package manager materializes into it, is
  100% independent per worktree. If Forge runs `pnpm install` (or npm/yarn) inside each new
  worktree, each one grows its own full `node_modules` tree.
- **pnpm's content-addressable store only dedupes disk *bytes*, not NFS *entries*.** pnpm
  hard-links package contents from one global store (`~/.local/share/pnpm/store`) so ten
  projects on the same dependency version cost one copy of the *bytes* — but "the directory
  structure is recreated for each project so that Node's module resolution algorithm can find
  the right dependencies," i.e., every worktree still gets its **own full set of directories,
  symlinks, and hardlink inodes** inside `node_modules/.pnpm/...`. On local disk this is
  nearly free (hardlinks are cheap metadata ops); **on NFS/EFS this is not free** — each
  hardlink/symlink/directory creation is still a network metadata round-trip, and a
  medium-sized monorepo's `node_modules` easily runs 10-20k+ entries. ([pnpm — Motivation](https://pnpm.io/motivation), [pnpm — Symlinked node_modules structure](https://pnpm.io/symlinked-node-modules-structure))
  This reconciles the numbers in the prompt: 13 worktrees × (a few thousand tracked source
  files + a `node_modules` in the ~5-10k range) plausibly totals ~114k, even though tracked
  source in this actual repo is only 3,003 files — **the dominant contributor is almost
  certainly `node_modules`, not source.**
- **`pnpm install --frozen-lockfile` cost per worktree** is therefore twofold: (a) the CPU/NFS
  cost of materializing thousands of hardlinks+symlinks over the network on every fresh
  worktree, and (b) the fact that this cost is paid *again* per worktree even though the
  global store already has the bytes cached — there is no cross-worktree install-skip unless
  you point every worktree's `node_modules` at one shared location.
- **Turborepo caching is not the fix for this** — it's an orthogonal win. `turbo` detects a
  `git worktree` setup and, when no explicit `cacheDir` is configured, redirects the cache to
  the *main* worktree's `.turbo/cache`, so **task-output caching** is already shared across
  worktrees on the same machine (and further via remote cache/`TURBO_TOKEN`+`TURBO_TEAM`).
  ([Turborepo docs — Caching](https://turborepo.dev/docs/crafting-your-repository/caching), [vercel/turborepo discussion #7884](https://github.com/vercel/turborepo/discussions/7884)) But this only
  caches *build/test outputs*, not `node_modules` — it does nothing about the install-time
  entry-count problem above.
- **Practical mitigation, if per-worktree installs are kept at all:** point every worktree's
  `node_modules` at pnpm's shared **global virtual store** (symlink-only structure — each
  project's `node_modules` becomes symlinks into `<store>/links/`) rather than the
  default hardlink-per-project layout; this trades "N sets of directories+hardlinks" for
  roughly "N symlinks per top-level dependency," which is a large reduction in NFS entries
  per worktree, though it's still not zero. Better still: don't run `pnpm install` inside a
  worktree at all — see the recommendation in §5.

---

## 5. Policy comparison for the fleet

Container facts pulled from `CLAUDE.md` that bear directly on this decision: containers get
**Fargate ephemeral (task-local) storage that's wiped whenever the task restarts** (deploy,
OOM, health-restart, reprovision — this happens routinely per the existing container
orchestration notes), while `/mnt/efs/users/{user_id}/` is the durable, persistent NFS mount.
AWS's own docs confirm the ephemeral-storage lifecycle is tied strictly to the task: storage
and all its data are gone the moment the task stops. ([AWS — Fargate task ephemeral storage](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-storage.html))

### (a) Task-local scratch (ephemeral), outside the workspace, one worktree per active task, removed at PR-open/merge

- **Matches every commercial coding-agent pattern in §1** — Codex, Copilot, Cursor, Devin,
  Jules all treat the per-task checkout as fully disposable compute, never a persistent asset.
- **Matches this platform's own precedent**: the container's SQLite state already lives on
  task-local NVMe and is restored from S3 at boot (`local-sqlite/` boot script per CLAUDE.md)
  — i.e., "ephemeral local disk + durable remote restore" is already the platform's idiom,
  not a new pattern.
- **What breaks for parallel work:** nothing, as long as each active task gets its own
  scratch worktree — that's the whole point, and it's exactly what git worktrees already do.
- **What happens on container restart:** the scratch worktrees vanish along with all local
  disk. Any *uncommitted* diff in a mid-task worktree is lost — this is the real cost of this
  policy, and it requires a hard rule: **commit + push before any long-running/interruptible
  step**, not just at task end. It also leaves the *main* repo's `.git/worktrees/<name>/`
  administrative records (which live on EFS, since the main clone's `.git` is presumably on
  EFS) pointing at now-nonexistent local paths — **boot must run `git worktree prune`
  unconditionally** to clear those stale records before creating any new worktree, or `git
  worktree add` for a same-named task will collide with a dangling entry.
- **Net effect on the EFS entry-count problem:** eliminates it almost entirely — the EFS tree
  never sees more than the one main clone's tracked files, because every worktree's *working
  tree* (including any per-worktree `node_modules`) lives on local NVMe, not NFS.

### (b) Checkouts stay in the workspace on EFS, with a lifecycle rule + boot sweep

- Closest to current behavior (Forge already does this; the only change is adding the sweep
  from §3 and a cone-mode sparse-checkout default from §2).
- **What breaks for parallel work:** nothing structurally — this is still real `git worktree`,
  so concurrent tasks don't collide. The problem this policy does NOT fix is the underlying
  **NFS metadata cost per worktree**: even a well-swept fleet with, say, 5 worktrees alive at
  steady state still pays 5x the metadata-op cost of 1 on every recursive walk, `git status`,
  and (worst of all) `pnpm install`. Sparse-checkout narrows this per-worktree, but multiplies
  by however many concurrent tasks exist.
- **What happens on container restart:** nothing is lost — EFS is durable, so mid-task
  uncommitted work survives a restart. This is policy (b)'s one real advantage over (a).
- **Net effect on the EFS entry-count problem:** bounds it (no more unbounded growth) but
  doesn't reduce steady-state cost the way (a) does; still exposed to the industry-consensus
  "don't run git-heavy or `node_modules`-heavy workloads on NFS" guidance from §2 (GitLab
  deprecating NFS for Gitaly; AWS's own "not suitable" language for EFS).

### (c) Single clone, branch switching, no worktrees

- **What breaks for parallel work:** everything the whole worktree pattern exists to avoid —
  two tasks can't have different branches checked out simultaneously; a `git checkout`
  mid-task from a second concurrent agent invocation silently rewrites the first task's
  working tree under it. Given Forge is presumably meant to work multiple issues without
  hard-serializing, this policy is a functional regression, not just a hygiene tradeoff — none
  of the products surveyed in §1 use single-clone-branch-switching for concurrent agent tasks
  (Aider's community worktree pattern exists specifically because single-clone branch
  switching doesn't support concurrent agents).
- **What happens on container restart:** whatever branch was checked out survives (EFS is
  durable), but there's still just one working tree, so this doesn't change the restart story
  meaningfully — it just removes the "which worktree was I in" bookkeeping at the cost of
  serializing all work.
- **Net effect on entry count:** trivially solves it (1 checkout, ever) but at the cost of
  concurrency, which is presumably why worktrees were adopted in the first place. Reject
  unless the fleet's actual usage pattern turns out to be "one task at a time per container" —
  worth a one-line sanity check against how Forge is actually invoked before ruling this out
  entirely, but nothing in the provided context suggests strict serialization is acceptable.

### Recommendation: (a), with the EFS main clone kept as the single source of truth

Concrete rule text for an agent skill:

> **Where checkouts live:** the durable main clone stays on EFS at its current path. Every
> per-task checkout is a `git worktree add` rooted at task-local scratch storage (Fargate
> ephemeral disk), never inside the EFS-mounted workspace tree. Sparse-checkout to the
> app(s) the task actually touches when the repo has more than ~2k tracked files.
>
> **How many:** one worktree per *currently active* task. Before creating a new one, run
> `git worktree prune` (clears stale entries from a prior container restart), then check the
> live count against a ceiling (e.g. 6); if at the ceiling, run the sweep below before adding
> another.
>
> **When removed:** immediately when the task's PR is opened and pushed (the durable record
> is now the remote branch/PR, not the local worktree) — OR immediately when a `git status`
> check at task end shows the worktree clean. Never leave a worktree past task completion
> "just in case."
>
> **Discipline required for restart-safety:** commit and push before any step that could
> outlive the container process (long shell commands, anything crossing a poll/retry
> boundary). Local scratch does not survive an OOM kill, deploy, or health-restart — treat
> "not yet pushed" as "not yet safe."
>
> **Boot-time sweep (runs unconditionally, every container start):** `git worktree prune`
> first (clears dangling records from the wiped scratch disk), then for every worktree that
> somehow *does* still exist (e.g., a same-boot restart that didn't wipe disk) — apply the
> merged/dirty/unpushed checks from §3 and remove anything clean-and-merged.
>
> **Post-merge sweep:** on the PR-merged signal for a branch this agent owns, remove that
> task's worktree if it's still around and clean; otherwise flag it (dirty-but-merged is a
> bug in the agent's own flow, not something to auto-delete).

Skipped for now: cross-worktree shared `node_modules` (pnpm global virtual store) — add if
per-worktree `pnpm install` cost (not just entry count) becomes a measured bottleneck; the
scratch-disk move already removes the NFS cost of installs, which was the sharper edge.
