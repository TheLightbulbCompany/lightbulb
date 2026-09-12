# A — How remote/cloud agent runtimes handle the working tree and state

Research date: **2026-09-12**. All URLs fetched on that date. Claims are attributed
to the vendor doc that states them; where I could not verify something I say so
explicitly rather than infer.

Context this answers: Lightbulb runs one always-on ECS Fargate container per
workspace (1 vCPU / 4 GB, Spot), agent working files on **EFS/NFS** at
`/home/node/.openclaw/workspace-<agentId>/`. One agent (Forge) had 1 clone +
13 `git worktree` checkouts of a 16k-file repo = ~114k directory entries, and a
backend recursive walk over that NFS tree took ~150 s and froze the platform.

---

## 0. The one-paragraph answer

**No production agent runtime puts the agent's working tree on a shared network
filesystem.** Across all nine vendors surveyed, the working tree lives on the
sandbox's own block device (VM disk / container overlay / microVM rootfs). What
varies is only *how the tree is reconstituted*: a fresh `git clone` per task
(Claude Code web, Codex cloud, Cursor on cache-miss), a **disk/filesystem
snapshot** restored onto a fresh machine (Claude Code env cache, Codespaces
prebuilds, Devin machine snapshots, Cursor Builds, Daytona snapshots, Modal
directory snapshots, E2B pause/resume), or a **tarball in object storage**
restored at boot (Gitpod Classic). Network-attached storage appears in exactly
two places in this survey, and both are explicitly *not* the working tree:
Daytona **Volumes** (S3-backed, for shared datasets) and Modal **Volumes**
(which the docs explicitly say are "not a general-purpose/POSIX filesystem" and
degrade linearly with file count). Lightbulb already applies the correct
pattern to SQLite — task-local NVMe, restored from S3, replicated by litestream.
The gap is that the *working tree* never got the same treatment.

---

## 1. Anthropic — Claude Code on the web / cloud sessions

Sources: <https://code.claude.com/docs/en/claude-code-on-the-web>,
<https://code.claude.com/docs/en/cloud-environments>,
<https://code.claude.com/docs/en/worktrees> (all fetched 2026-09-12).

| dimension | answer |
|---|---|
| working tree | **ephemeral**, on the session VM's own disk |
| durable store | **git** (branch pushed through a credential proxy) + server-side conversation history |
| repo obtained | **fresh clone per session** |
| environment reuse | **filesystem snapshot** of the post-setup-script VM, ~7-day expiry |
| idle/stop | VM reclaimed after inactivity; reopening provisions a **fresh VM** |
| resume | conversation history restored; in-flight shell/subagent work is **not** |

Exact wording worth keeping:

- "In Anthropic-hosted environments, each session gets a fresh virtual machine
  (VM) running Ubuntu 24.04 on x86_64 … with your repository cloned and common
  toolchains pre-installed."
- "Cloud sessions start from a fresh clone of your repository."
- Resource ceilings: "4 vCPUs / 16 GB of RAM / 30 GB of disk" (stated as
  approximate and subject to change).
- Environment caching: "The setup script runs the first time you start a session
  in an environment. After it completes, **Anthropic snapshots the filesystem and
  reuses that snapshot as the starting point for later sessions.** New sessions
  start with your dependencies, tools, and Docker images already on disk, and
  skip the setup script step."
- Cache semantics: "The cache is a filesystem snapshot, so it keeps what the
  setup script writes to disk and loses anything that was only running."
- Cache invalidation: rebuilt "when you change the environment's setup script or
  allowed network hosts, and when the cache reaches its expiry after roughly
  **seven days**. Resuming an existing session never re-runs the setup script."
- Setup script must "finish within five minutes … so the environment cache can
  build."
- Idle: "Cloud sessions stop after a period of inactivity and the session's VM is
  reclaimed. … Reopen the session … to provision a fresh VM with your
  conversation history restored. Background work that was still running when the
  VM was reclaimed, such as subagents and shell commands, isn't restored."
- Credentials: "git credentials and signing keys stay outside the sandbox, and a
  proxy authenticates on the session's behalf with scoped credentials."

**No start/resume latency figures are published.** The CLI shows "a live
checklist of setup steps, such as cloning the repository and running your setup
script" during provisioning, which implies tens of seconds, but I could not find
a number.

### 1b. Local `--worktree` isolation — directly relevant to the Forge incident

This is the same mechanism Forge used, and Anthropic's docs describe a **lifecycle
and a sweeper**, which is exactly what the Lightbulb workspace lacks:

- Default location: `.claude/worktrees/<name>/`, branch `worktree-<name>`.
  Docs recommend adding `.claude/worktrees/` to `.gitignore`.
- Clean exit of an interactive session: "for an unnamed session, Claude removes
  the worktree and its branch automatically."
- **"Non-interactive runs with `-p` have no exit prompt, so Claude doesn't clean
  up their worktrees"** — i.e. headless agents accumulate worktrees by design
  unless something sweeps them. This is precisely the Forge failure mode.
- Sweeper: "Claude Code runs a periodic sweep that removes worktrees that Claude
  created for subagents and background sessions once they are older than your
  `cleanupPeriodDays` setting." The sweep leaves a worktree in place if it "still
  holds work: changed or untracked files, or unpushed commits."
- Subagent worktrees: "Each subagent gets a temporary worktree that Claude Code
  removes automatically when the subagent finishes without changes."
- And a direct statement about network paths:
  **"Claude Code never resumes into a worktree at a network path. Recreate the
  worktree at a local path."** (troubleshooting: `its recorded path has a network
  spelling`). That is the closest thing to a first-party "worktrees do not belong
  on network storage" statement I found.

---

## 2. OpenAI — Codex cloud

Sources: <https://learn.chatgpt.com/docs/environments/cloud-environment.md>
(canonical redirect target of `developers.openai.com/codex/cloud/environments`),
plus <https://openai.com/index/introducing-codex/> summary via search.

| dimension | answer |
|---|---|
| working tree | **ephemeral per task**, inside an OpenAI-managed container |
| durable store | **git** (proposes a PR) |
| repo obtained | "Codex creates a container and checks out your repo at the selected branch or commit SHA" |
| environment reuse | **container state cached up to 12 hours** |
| idle/stop | container torn down at task end; cache expires at 12 h |

- Base image: the default container is **`universal`**, "pre-installed with common
  languages, packages, and tools"; reference Dockerfile published at
  `openai/codex-universal`, image at `ghcr.io/openai/codex-universal:latest`.
  You can pin runtime versions ("Set package versions").
- Caching: "Codex caches container state for **up to 12 hours** to speed up new
  chats and follow-ups." On a cache hit it "checks out the branch specified for
  the chat" — so the cached container carries a *warm clone* that is then moved to
  the right commit.
- Invalidation: "automatically invalidates the cache if you change the setup
  script, maintenance script, environment variables, or secrets"; there is a
  manual **Reset cache**.
- Two scripts, deliberately split: the **setup script** runs once at container
  creation; the **maintenance script** runs "when resuming a cached container …
  useful when the setup script ran on an older commit and dependencies need to be
  updated." That is the pattern for keeping a warm cache correct across commits.
- Network: "Internet access is available during the setup script phase"; during
  the agent phase it is "off by default."
- The docs do not state in so many words that the working tree is discarded after
  a task; the surrounding material ("each task runs in its own cloud sandbox
  pre-loaded with your repository", "ephemeral Linux container") says so, but I
  am flagging that the *cached* container blurs it: within 12 h, follow-ups reuse
  the same warm disk.

**No published start/resume latency.**

---

## 3. GitHub Codespaces

Sources: <https://docs.github.com/en/codespaces/about-codespaces/deep-dive>,
<https://docs.github.com/en/codespaces/prebuilding-your-codespaces/about-github-codespaces-prebuilds>,
<https://docs.github.com/en/codespaces/setting-your-user-preferences/configuring-automatic-deletion-of-your-codespaces>,
<https://github.blog/engineering/infrastructure/githubs-engineering-team-moved-codespaces/>,
<https://github.blog/news-insights/product-news/codespaces-largest-repositories-faster/>.

| dimension | answer |
|---|---|
| working tree | **persistent per codespace**, on the VM's own disk at `/workspaces` |
| durable store | git + the VM's disk, with a hard 30-day retention clock |
| repo obtained | **shallow clone** into `/workspaces`, or a **prebuilt container template** from blob storage |
| idle/stop | stops after **30 min** idle (configurable 5–240 min) |
| deletion | stopped codespaces **auto-deleted after 30 days** by default (0–30 configurable) |

- "A virtual machine (VM) is created … A **shallow clone** of your repository is
  then made and cloned into the `/workspaces` directory."
- Persistence boundary is explicit and narrow: `/workspaces` survives
  stop/start/rebuild; "directories outside `/workspaces` (except `/tmp`) are tied
  to the lifecycle of the container … not preserved when you rebuild the
  container."
- Prebuilds: "source code, editor extensions, project dependencies, commands, and
  configurations have already been downloaded, installed, and applied." At create
  time GitHub "deploys an existing container snapshot on a fresh virtual
  machine"; the 2022 launch post says prebuilt **templates are stored in blob
  storage** and "the service will fetch a prebuilt template and attach it to an
  existing virtual machine."
- Guidance threshold: use prebuilds when "it currently takes more than 2 minutes
  to create a codespace."
- Prebuilds run `onCreateCommand` / `updateContentCommand` but **not**
  `postCreateCommand`; they refresh on every push (concurrency 1).
- GitHub's own numbers, from the engineering blog (their monorepo is "almost
  13 GB on disk"):
  - full clone **20 minutes** → shallow clone + background unshallow: **90 seconds**
  - end-to-end codespace creation: **45 minutes → 5 minutes** (shallow + caching)
    → **10 seconds** with prebuilds.
- Retention: "automatically deleted after they have been stopped and have
  remained inactive for 30 days"; "retention period is reset every time you
  connect"; setting 0 deletes immediately on stop. Deletion "happens irrespective
  of whether a codespace contains unpushed changes."

**Resume time after a stop is not published** in the docs I read.

---

## 4. Gitpod / Ona

Sources: <https://ona.com/docs/ona/environments/persistent-storage>,
<https://www.gitpod.io/docs/classic/user/configure/workspaces/workspace-lifecycle>
and <https://www.gitpod.io/docs/configure/self-hosted/latest/reference-architecture/_chunks/storage>
(via search summaries), <https://github.com/gitpod-io/gitpod/issues/8104>.

| dimension | answer |
|---|---|
| working tree | **persistent**, "persistent storage attached to the underlying VM" |
| durable store | the VM's attached disk while alive; **object storage tarball** when stopped (Gitpod Classic) |
| repo obtained | cloned into `/workspaces/<repo>`; **prebuilds snapshot the whole dev-container filesystem** |
| rebuild | repo at `/workspaces/<repo>` "persists via a bind mount" |
| delete | "storage is permanently removed"; archived environments keep storage and can be unarchived |

- Ona: "Everything on disk persists across environment stop and starts" —
  repository, home dir, shell history, installed packages.
- Prebuilds "snapshot the entire Dev Container filesystem — installed
  dependencies, built artifacts, and all files," so new environments "load from
  this snapshot instead of running setup from scratch."
- Gitpod Classic lifecycle: a backup is taken when a workspace stops and restored
  on restart; self-hosted docs describe workspace content going to **object
  storage** (a bucket per user) rather than to a shared filesystem.

**Caveat — I could not verify the "why block storage, not NFS" claim.**
Gitpod/Ona publish *what* they use (VM-attached disk + object-storage backup) but
I found **no** published post or doc stating a rationale of "NFS is unsuitable for
git/node_modules." Issue #8104 ("Prototype using persistent volumes for storage of
user data") is an exploration with open questions and no published performance
conclusion. Do not cite Gitpod as evidence for the NFS argument; cite it only as
evidence of the architecture (local disk + object storage).

---

## 5. Daytona

Source: <https://www.daytona.io/docs/en/persistence/>,
<https://www.daytona.io/docs/en/snapshots/>, <https://www.daytona.io/>.

| dimension | answer |
|---|---|
| working tree | **on the runner's disk**; class-dependent on stop |
| durable store | snapshots (point-in-time) + **S3-backed Volumes** for shared data |
| idle/stop | auto-stop **15 min** (Container/GPU), auto-pause **60 min** (VM) |
| archive | auto-archive after **7 days** stopped → filesystem moved to object storage |

- Container sandboxes: "Filesystem stays on runner while stopped, counts against
  quota. Archive moves it to object storage and frees the quota."
- VM sandboxes: "Stopping offloads it to nearby storage and releases disk quota,
  starting restores it." GPU sandboxes are "ephemeral by design: deleted on stop."
- Snapshots: "persistent, point-in-time captures of sandbox state, including the
  filesystem, installed packages, dependencies, and settings," independent of the
  source sandbox — **cold** (filesystem) or **hot** (filesystem + memory, VM only).
- Volumes: "**S3-backed mounts** that persist independently of any sandbox,"
  shareable across sandboxes — positioned for "datasets, dependencies,
  repositories, or other files" shared *across tasks*, i.e. explicitly a sharing
  mechanism, not the hot working tree.
- Start latency: Daytona markets **"sub-90ms"** sandbox creation. Note this is a
  marketing claim tied to pre-built snapshots + warm pools; a third-party
  benchmark (medium.com/@kacperwlodarczyk) reports 71 ms create / 67 ms exec /
  59 ms cleanup. Treat 90 ms as best-case-from-snapshot, not general.

---

## 6. E2B

Source: <https://docs.e2b.dev/sandbox/persistence>, plus E2B marketing pages.

| dimension | answer |
|---|---|
| working tree | **microVM-local** (Firecracker rootfs) |
| durable store | **VM snapshot** (filesystem + memory) |
| idle/stop | explicit `pause()`; paused sandboxes kept **indefinitely, no TTL** |
| resume | **~1 second**; pause costs **~4 s per GiB of RAM** |

- Pause preserves "all the files in the sandbox's filesystem and all the running
  processes, loaded variables, data, etc."
- `keepMemory: false` (JS) / `keep_memory=False` (Py) does a **filesystem-only**
  pause — lighter snapshot, cold boot on resume.
- Runtime caps: 24 h continuous (Pro), 1 h (Hobby); the clock resets across a
  pause/resume cycle.
- Firecracker snapshot-restore is the mechanism; marketing claims sub-200 ms
  sandbox init and 5–30 ms Firecracker-level restore, but the docs' own number
  for a full E2B resume is **~1 s**. Use the ~1 s figure.
- Known sharp edge: e2b-dev/E2B issue #884 reports file changes not persisting
  after the second resume onward. Treat "snapshot is a database" with caution.

---

## 7. Modal

Sources: <https://modal.com/docs/guide/volumes>,
<https://modal.com/blog/directory-snapshots-resumable-project-state-for-sandboxes>,
<https://modal.com/docs/guide/sandbox-snapshots>, <https://modal.com/docs/guide/memory-snapshots>.

Modal is the most directly transferable vendor, because it ships **three** kinds
of persistence and documents when each is wrong.

| kind | what it captures | retention |
|---|---|---|
| **Filesystem snapshot** | whole sandbox FS as a diff from the base image | (blog: indefinite; a secondary source says a 30-day default TTL now applies — **conflicting, verify before relying**) |
| **Directory snapshot** | *one directory*, mountable into a fresh sandbox | 30 days after last use |
| **Memory snapshot** | filesystem + full process tree | expires **7 days**; restorable **only on the same instance type** |

The directory-snapshot rationale is the single best-stated version of the design
lesson for Lightbulb:

> "full filesystem snapshots treat the Sandbox state as one atomic unit"

so an agent that wants to keep *project* state has to keep the whole stale
environment with it. Directory snapshots split them: base image carries the
toolchain, a snapshot of `/project` carries the work, and "security patches,
updates to your agent harness, or runtime upgrades no longer require discarding
an outdated full filesystem snapshot."

And Modal's **Volumes** doc is the closest thing in this survey to a vendor
saying out loud "do not put a metadata-heavy tree on network storage":

- optimized for "**write-once, read-many** I/O workloads, like creating machine
  learning model weights and distributing them for inference"
- "Volumes perform best when they contain **less than 50,000 files and
  directories**"
- "**Latency to attach or modify a Volume scales linearly with the number of
  files**"
- hard limit "**500,000 inodes** (files, directories and symbolic links) per Volume"
- "Concurrent modifications of the same files should be avoided. Last write wins"
- explicitly "not a general-purpose/POSIX filesystem"; distributed file locking
  unsupported.

Forge's workspace was **114,000 directory entries** — over twice Modal's
recommended ceiling for a *network volume*, sitting on one.

---

## 8. Devin (Cognition)

Sources: <https://cognition.com/blog/blockdiff>, <https://docs.devin.ai/>
(machine-snapshot pages reached via search; the direct URL I tried 404'd, so the
doc quotes below come from search-surfaced Devin documentation text and should be
re-verified against docs.devin.ai before being quoted externally).

| dimension | answer |
|---|---|
| working tree | **persistent-looking, actually snapshot-restored**: every session boots from a saved machine state |
| durable store | **VM disk snapshot** (their own `blockdiff` format) |
| repo obtained | pre-cloned *into the snapshot*; a per-session startup command does `git pull` / `npm install` |
| idle/stop | sleep/wake is one of the three stated snapshot use cases |

- "Machine Snapshots are Devin's built-in save states. After you take a snapshot,
  every future session can start from that exact machine state, including
  everything you have downloaded and installed." Teams use them to "preinstall
  dependencies, pre-authenticate into services, or have repositories pre-cloned."
- "Devin's Workspace resets to a saved machine state at the start of every
  session" — so it is *reset-to-snapshot*, not accumulate-forever. Startup
  commands (`cd repo && git pull && npm install`) reconcile the snapshot with
  HEAD, which is the same job Codex's maintenance script does.
- `blockdiff` (the engineering post, first-party, with numbers):
  - snapshotting on EC2 took "**30+ minutes**"; blockdiff does it in "just a
    couple of seconds – a **200x speed-up**"
  - measured: "Reading/writing 20 GB of data: ~6.5 s" vs "Creating 20 GB snapshot
    with blockdiff: **~200 ms**"
  - three drivers: **dev-environment reuse**, **sleep/wake**, **disk rollback**
  - rejected alternatives, with reasons: qcow2 ("quite slow for large files"),
    OverlayFS ("17x more storage consumption, 6x slower", Docker incompat), ZFS
    (mounting/rollback limits), binary diff ("30–60 seconds" to scan a 128 GB disk)
  - mechanism: FIEMAP extent maps + **XFS reflink** copy-on-write; metadata-only.

The lesson Cognition paid for: **making the disk snapshot cheap is what makes
sleep/wake and reset-per-session viable.** They built a file format rather than
accept a 30-minute snapshot.

---

## 9. Cursor cloud agents (formerly background agents)

Sources: <https://cursor.com/docs/cloud-agent>, <https://cursor.com/docs/cloud-agent/setup>,
<https://www.learncursor.dev/learn/cursor-agents/cloud-agent-environment-json>,
<https://developers.cloudflare.com/sandbox/tutorials/cursor-cloud-agents/>.

| dimension | answer |
|---|---|
| working tree | per-agent VM disk; prepared by a **Build** |
| durable store | git branch + PR; **repository snapshot cache** |
| repo obtained | "Cursor starts from the configured base, **clones every repository** at its default branch, and runs `install` to completion" — or restores a snapshot |
| idle/stop | not published by Cursor; the self-hosted protocol has an idle timeout + max run lifetime |

- "Cursor manages VM provisioning, isolation, snapshots, startup, artifacts, and
  capacity for every Cloud Agent."
- Build/agent split, stated plainly: "**Builds preserve disk state only.** Running
  processes, exported shell variables, and in-memory caches don't continue into an
  agent run." Hence `install` (durable, idempotent, runs at Build) vs `start`
  (per-run: Docker daemon, databases, tunnels).
- Install scripts "must be idempotent" since they "run for every Build and may run
  on previously prepared disk state."
- Explicit anti-pattern: "**Don't `COPY` the project in your Dockerfile.** Cursor
  manages the workspace and checks out the correct commit itself." — i.e. the
  environment image and the working tree are deliberately separate layers.
- Repository snapshot cache (from Cloudflare's docs for running Cursor cloud
  agents on self-hosted machines — **Cloudflare's implementation of Cursor's
  protocol**, so attribute carefully): "Repository snapshots are an optional cache
  for repository-bound agents. **A snapshot stores the post-clone working tree in
  R2.**" And: "**A cache miss performs a normal Git clone. It does not prevent the
  agent from starting.**" Container lifecycle there: "The container exits after the
  configured idle timeout. The Durable Object also enforces a maximum run
  lifetime." One container per Cursor session.

That cache-miss line is the design principle to steal: **the cache is an
optimization, never a source of truth.**

---

## 10. Evidence that NFS / EFS is wrong for git + node_modules trees

### 10.1 First-party AWS

<https://docs.aws.amazon.com/efs/latest/ug/performance.html> (fetched 2026-09-12):

- Regional + General Purpose + Elastic: read latency **~1 ms**, write **~2.7 ms**.
  Footnote: "Latency values shown represent **best-case** performance under
  optimal conditions."
- Elastic throughput default ceilings: 250,000 read IOPS for frequently accessed
  data, 90,000 for infrequently accessed, 50,000 write IOPS.
- "Every Network File System (NFS) request is accounted for as 4 kilobyte (KB) of
  throughput, or its actual request and response size, whichever is larger."
  (So a `getattr` on a 200-byte file bills like a 4 KB read.)

<https://docs.aws.amazon.com/efs/latest/ug/performance-tips.html> — this page is
effectively AWS telling you not to do what Lightbulb is doing:

- "This distributed architecture results in a **small latency overhead for each
  file operation**."
- "**File open, close, and metadata operations generally cannot be made
  asynchronously or through a pipeline.**"
- "**Each round trip (file open, file close) can take as much time as reading or
  writing megabytes of bulk data.**"
- "**We recommend keeping the number of directories on your file system to less
  than 10,000, if possible.** Use nested subdirectories as much as possible."
- "When performing a listing (`ls`) on very large directories (**over 100k
  files**) that are being modified concurrently, **Linux NFS clients can hang, not
  returning a response.**" (fixed in kernel 5.11 / backports)
- "When listing a directory, **avoid getting file attributes if they are not
  required**, because they are not stored in the directory itself." — a recursive
  Python walk that stats every entry does exactly this.
- "NFSv4.1 provides better performance for parallel small-file read operations
  (greater than 10,000 files per second) compared to NFSv4.0 (less than 1,000
  files per second)."
- Do not disable the attribute cache or lookup cache: "has a **very large**
  performance impact."
- The recommended way to go fast is parallelism: "Distribute the workload across
  **10 or more NFS clients, with at least 64 threads per client**." A single-
  threaded `os.walk` from one container is the worst case this page describes.

**Arithmetic (my inference, not a vendor claim):** 114,000 entries × ~1 ms
best-case round trip, issued serially, ≈ **114 s**. The observed ~150 s is
consistent with a serial metadata walk at EFS's documented per-op latency, with
no bandwidth problem at all. This is a latency × operation-count failure, so
"provision more throughput" would not have fixed it.

### 10.2 A published post-mortem: git temp files starving EFS metadata IOPS

<https://dev.to/tielec-takashi/how-git-temp-files-killed-our-jenkins-performance-efs-metadata-iops-hell-3ff4>
(third-party, engineering write-up):

- `tmp_pack_*` files from Jenkins git clones accumulated across build dirs, ~15 GB
  of orphans.
- Symptoms: `git clone` timing out with "Bad file descriptor", 504 Gateway
  Timeouts, sluggish UI, EFS **throughput utilization at 100% from 00:00–03:00**.
- Root cause as stated: "**EFS was starving on metadata IOPS**" — not capacity.
- Fixes: switch Bursting → Provisioned (300 MiB/s), plus a **daily cleanup job**
  deleting stale `tmp_pack_*`. Stated lesson: "Storage size doesn't guarantee
  performance on EFS — file count and metadata operation frequency are critical."

### 10.3 Why git specifically is pathological on NFS

- LKML thread "NFS: low read/stat performance on small files"
  (<https://lkml.iu.edu/hypermail/linux/kernel/1203.2/03538.html> and replies):
  opening a file requires "a number of **synchronous** RPC calls … look up the
  file, check access permissions and possibly revalidate its cache. All these
  tasks have to be done **in series** … the latency of each task is limited by the
  round-trip time to the server."
- Git's own loose-refs/loose-objects layout is many tiny files; the standard
  mitigation is `git gc` / `packed-refs` to collapse them into one file, which is
  a well-known fix precisely because per-file round trips dominate on a
  high-latency FS.
- Atlassian documents a dedicated procedure for **testing NFS disk access speed
  for Bitbucket Data Center and git operations**
  (<https://confluence.atlassian.com/bitbucketserverkb/testing-nfs-disk-access-speed-for-bitbucket-data-center-and-git-operations-1044804304.html>)
  — i.e. even a vendor that *does* ship git-on-NFS treats NFS latency as the
  primary risk to benchmark before deploying.
- AWS re:Post has a standing question "Why do EC2 and ECS instances with EFS
  mounted perform poorly when reading and writing a large number of small files?"
  (<https://repost.aws/questions/QUm_ED7FMwRd6F2vXuihyuEw/...>) — I got a 403
  fetching it directly, so I am citing its existence and title only, not its
  answers.

### 10.4 The multiplier nobody in this survey has

`git worktree` does **not** share the checkout — each worktree is a full working
copy of every tracked file (only `.git` object storage is shared). 14 checkouts of
a 16k-file repo ≈ 224k tracked files before `node_modules`; the observed 114k
entries is consistent with partially-populated trees. No surveyed vendor gives one
agent 13 simultaneous long-lived checkouts on shared storage: the per-task
isolation unit everywhere is **a container or VM**, one task each.

---

## What to copy

Eight practices, each with the vendor that demonstrates it.

1. **Move the working tree off EFS onto the task-local NVMe volume — the same
   place SQLite already lives.**
   Every runtime surveyed puts the tree on the sandbox's own block device:
   Codespaces clones into `/workspaces` on the VM
   (docs.github.com/…/deep-dive); Ona uses "persistent storage attached to the
   underlying VM"; Claude Code, Codex, Cursor and E2B all clone into the
   container/microVM. The one vendor that *does* offer network storage warns you
   off this exact shape: Modal Volumes "perform best when they contain less than
   50,000 files and directories", "latency to attach or modify a Volume scales
   linearly with the number of files", "not a general-purpose/POSIX filesystem"
   (modal.com/docs/guide/volumes). Lightbulb's workspace had 114k entries.
   EFS keeps `openclaw.json`, skills, media, logs — small, low-fanout, genuinely
   shared state. It does not keep checkouts.

2. **Make the working tree derivable, never durable: git is the store of record,
   and any cache is allowed to miss.**
   Claude Code web: "Cloud sessions start from a fresh clone of your repository."
   Codex: "Codex creates a container and checks out your repo at the selected
   branch or commit SHA." Cursor's snapshot cache is explicitly optional —
   "**A cache miss performs a normal Git clone. It does not prevent the agent from
   starting.**" (Cloudflare's Cursor cloud-agent runner docs). If losing a
   checkout is a re-clone rather than an incident, the storage tier stops being a
   correctness problem.

3. **Restore the expensive part from a snapshot/object store at boot, rather than
   mounting it.**
   This is Lightbulb's own litestream pattern applied one directory over, and it
   is what Gitpod Classic does for workspaces (backup on stop → object storage →
   restore on start), what Codespaces prebuilds do (prebuilt template in blob
   storage "attached to an existing virtual machine"), and what Devin does
   (`blockdiff` disk snapshots, 20 GB in ~200 ms vs 30+ min on EC2 —
   cognition.com/blog/blockdiff). Boot cost buys you a local-disk working set for
   the whole session.

4. **Clone shallow/partial, and expand lazily if at all.**
   GitHub's own monorepo is "almost 13 GB on disk"; full clone took **20 minutes**,
   shallow clone **90 seconds**, with the history unshallowed in the background
   afterwards (github.blog engineering post). Codespaces does a shallow clone by
   default. For a 16k-file repo the cheap modern version is
   `--filter=blob:none` + `sparse-checkout`, which cuts both bytes and,
   crucially here, **inodes on disk**.

5. **Cap and sweep worktrees; treat an unswept worktree as a leak.**
   Anthropic ships the exact lifecycle Lightbulb is missing: clean worktrees are
   removed on exit, subagent worktrees "removed automatically when the subagent
   finishes without changes", and a periodic sweep removes subagent/background
   worktrees older than `cleanupPeriodDays`, keeping only those with real work
   (code.claude.com/docs/en/worktrees). The same doc warns that
   "**Non-interactive runs with `-p` … don't clean up their worktrees**" — which
   is precisely how a headless agent like Forge reaches 13. Also note Anthropic's
   flat statement: "**Claude Code never resumes into a worktree at a network
   path.**" Add a per-agent concurrent-checkout cap and a sweeper, and expect the
   agent to `git worktree remove` on task completion.

6. **Split "environment state" from "project state" so either can be replaced
   without discarding the other.**
   Modal built Directory Snapshots for exactly this: "full filesystem snapshots
   treat the Sandbox state as one atomic unit," so patching the base image meant
   throwing away project work; directory snapshots let `/project` ride into a
   fresh, updated base. Cursor says the same operationally: "Don't `COPY` the
   project in your Dockerfile. Cursor manages the workspace and checks out the
   correct commit itself." For Lightbulb: toolchain in the ECR extended image,
   project dirs in a per-agent snapshot, config on EFS — three tiers, three
   lifecycles.

7. **Cache the setup work behind an explicit key and an explicit expiry, and give
   the cache a reconcile step.**
   Claude Code: setup script runs once, "Anthropic snapshots the filesystem and
   reuses that snapshot," rebuilt when the script or allowed hosts change or at
   **~7 days**. Codex: container state cached **12 h**, "automatically invalidates
   the cache if you change the setup script, maintenance script, environment
   variables, or secrets," plus a **maintenance script** that runs on resume
   "when the setup script ran on an older commit." Devin does the reconcile with
   per-snapshot startup commands (`cd repo && git pull && npm install`). A warm
   cache without a reconcile step is a correctness bug waiting to happen.

8. **Give idle workspaces a stop → archive → delete ladder with published clocks.**
   Codespaces: 30-min idle stop, 30-day retention, then deletion — and it deletes
   "irrespective of whether a codespace contains unpushed changes." Daytona:
   15-min auto-stop (container), 60-min auto-pause (VM), **7-day auto-archive to
   object storage**. E2B: explicit pause (~4 s/GiB), resume ~1 s, paused sandboxes
   "kept indefinitely." Lightbulb already scales trials to zero; what is missing is
   the second and third rungs — where the working tree goes when compute stops,
   and when it is allowed to be deleted. A 114k-entry tree that nothing ever
   prunes is the direct cause of the incident.

**Bonus, free and immediate:** never walk the agent's tree recursively from the
backend, on the event loop or off it. No surveyed vendor's control plane
enumerates the agent's working tree; state comes from git or from the agent
reporting it. If the backend needs to know what changed, run
`git status --porcelain` / `git ls-files` **inside** the container, or read an
index the agent maintains. AWS's own advice for the case where you must
enumerate: "avoid getting file attributes if they are not required," and
parallelize across "10 or more NFS clients, with at least 64 threads per client"
— neither of which a single-threaded backend walk can do.

---

## What I could not verify

- **Start/resume latency** for Claude Code cloud sessions, Codex cloud, Cursor
  cloud agents, and Codespaces-after-stop. None of the four publish numbers. Only
  Codespaces publishes *creation* numbers, and only via the engineering blog.
- **A published rationale from Gitpod/Ona (or Codespaces) for block storage over
  NFS.** They document the architecture; I found no doc stating the reasoning.
  Do not attribute that argument to them.
- **Modal filesystem-snapshot retention**: the Directory Snapshots blog says
  filesystem snapshots "persist indefinitely" while a secondary source reports a
  new 30-day default TTL. Re-check modal.com/docs/guide/sandbox-snapshots before
  depending on it.
- **Devin doc quotes**: `docs.devin.ai/product-guides/machine-snapshots` 404'd for
  me; the Devin documentation quotes above came through search surfacing. The
  `blockdiff` numbers are first-party and solid.
- **AWS re:Post answers** on EFS small-file performance: 403 on fetch; cited by
  title only.
- **Whether Codex's cached container truly discards the working tree between
  tasks** within the 12-hour window. The docs describe re-checkout on the cached
  container, which implies reuse, not a fresh tree.

## Sources

- https://code.claude.com/docs/en/claude-code-on-the-web
- https://code.claude.com/docs/en/cloud-environments
- https://code.claude.com/docs/en/worktrees
- https://learn.chatgpt.com/docs/environments/cloud-environment.md
- https://openai.com/index/introducing-codex/
- https://docs.github.com/en/codespaces/about-codespaces/deep-dive
- https://docs.github.com/en/codespaces/prebuilding-your-codespaces/about-github-codespaces-prebuilds
- https://docs.github.com/en/codespaces/setting-your-user-preferences/configuring-automatic-deletion-of-your-codespaces
- https://docs.github.com/en/codespaces/setting-your-user-preferences/setting-your-timeout-period-for-github-codespaces
- https://github.blog/engineering/infrastructure/githubs-engineering-team-moved-codespaces/
- https://github.blog/news-insights/product-news/codespaces-largest-repositories-faster/
- https://ona.com/docs/ona/environments/persistent-storage
- https://www.gitpod.io/docs/classic/user/configure/workspaces/workspace-lifecycle
- https://github.com/gitpod-io/gitpod/issues/8104
- https://www.daytona.io/docs/en/persistence/
- https://www.daytona.io/docs/en/snapshots/
- https://docs.e2b.dev/sandbox/persistence
- https://github.com/e2b-dev/E2B/issues/884
- https://modal.com/docs/guide/volumes
- https://modal.com/blog/directory-snapshots-resumable-project-state-for-sandboxes
- https://modal.com/docs/guide/sandbox-snapshots
- https://modal.com/docs/guide/memory-snapshots
- https://cognition.com/blog/blockdiff
- https://docs.devin.ai/
- https://cursor.com/docs/cloud-agent
- https://cursor.com/docs/cloud-agent/setup
- https://developers.cloudflare.com/sandbox/tutorials/cursor-cloud-agents/
- https://docs.aws.amazon.com/efs/latest/ug/performance.html
- https://docs.aws.amazon.com/efs/latest/ug/performance-tips.html
- https://dev.to/tielec-takashi/how-git-temp-files-killed-our-jenkins-performance-efs-metadata-iops-hell-3ff4
- https://lkml.iu.edu/hypermail/linux/kernel/1203.2/03538.html
- https://confluence.atlassian.com/bitbucketserverkb/testing-nfs-disk-access-speed-for-bitbucket-data-center-and-git-operations-1044804304.html
