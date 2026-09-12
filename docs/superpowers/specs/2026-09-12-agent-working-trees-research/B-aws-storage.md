# B — AWS-native storage options for a fast per-task working tree with durable state

Research date: **2026-09-12**. All prices us-east-1 unless stated. Everything sourced from AWS
docs/pricing/blogs; third-party or inferred claims are marked **[unverified]**.

## 0. The measurement this has to explain

114,000 directory entries walked in ~150 s = **760 ops/s = ~1.3 ms per operation**. AWS publishes EFS
General Purpose read latency as **~1 ms** and write latency as **~2.7 ms**
(<https://docs.aws.amazon.com/efs/latest/ug/performance.html>). So the observed number is *exactly*
one serialized NFS round trip per entry. It is not a throughput problem, not a burst-credit problem,
and **not a throughput-mode problem**: the EFS performance table shows the **same ~1 ms / ~2.7 ms
latency for Bursting, Provisioned and Elastic**. Switching to Elastic throughput will not move this
number. Only (a) removing the network from the path, or (b) parallelising the walk, will.

AWS's own EFS performance tips say this in as many words:

> "This distributed architecture results in a small latency overhead for each file operation. Because
> of this per-operation latency, overall throughput generally increases as the average I/O size
> increases…"
> "Each round trip (file open, file close) can take as much time as reading or writing megabytes of
> bulk data."
> "We recommend keeping the number of directories on your file system to less than 10,000, if possible."
> — <https://docs.aws.amazon.com/efs/latest/ug/performance-tips.html>

A git clone + 13 worktrees is the archetype of what that guidance says not to put on EFS.

---

## 1. Fargate ephemeral storage

Source: <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-storage.html>

| Property | Value |
|---|---|
| Default (platform version 1.4.0+, Linux) | **minimum 20 GiB** per task |
| Maximum | **200 GiB**, via `ephemeralStorage.sizeInGiB` in the task definition |
| Shared with | The pulled **compressed AND uncompressed container image** counts against the 20 GiB. Subtract image size to get usable space. |
| Encryption | AES-256 with an AWS-owned key by default, since 2020-05-28; customer-managed KMS keys supported (<https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-storage-encryption.html>) |
| Observability | Reserved/used reported via task metadata endpoint v4 and Container Insights, for tasks launched 2022-11-18 or later |
| Lifetime | **Task lifetime only.** Destroyed when the task stops — including every rolling deploy, Spot interruption, OOM kill and health-check restart. |
| Windows | pv 1.0.0+ same 20 GiB / 200 GiB numbers; bind-mount sharing between containers is **not** supported on Windows |

**Price**: `$0.0000000308 per GB per second` = **$0.00011088 per GB-hour ≈ $0.081 per GB-month**,
charged only above the included 20 GB (<https://aws.amazon.com/fargate/pricing/>). For reference the
same page gives Linux/x86 compute as `$0.000011244 per vCPU second` (**$0.04048/vCPU-hr**) and
`$0.000001235 per GB second` (**$0.004446/GB-hr**) — so the 1 vCPU / 4 GB box is **$0.0583/hr =
~$42.6/month on-demand**, and Fargate Spot is described only as "up to a 70% discount" (no published
per-unit Spot rate).

**Performance**: AWS publishes **no IOPS/throughput figure for Fargate ephemeral storage**. The one
hard clue is in the EBS-on-Fargate page: *"the Amazon EBS performance limit for Fargate task is
shared between the task's ephemeral storage and the attached volume"*
(<https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ebs-fargate-performance-limits.html>).
That sentence implies ephemeral storage is **EBS-class network block storage, not instance-store
NVMe**. **[inference, not stated outright]** — worth flagging because `CLAUDE.md` currently describes
these as "task-local NVMe volumes"; that description is probably wrong, though the practical
consequence (local-block-device latency ≈ tens of µs, ~1000× better than 1.3 ms NFS) is unchanged.

---

## 2. Amazon EBS volumes attached to Fargate tasks (the 2024 ECS+EBS integration)

Launched **2024-01-11** (<https://aws.amazon.com/about-aws/whats-new/2024/01/amazon-ecs-fargate-integrate-ebs/>,
<https://aws.amazon.com/blogs/aws/amazon-ecs-supports-a-native-integration-with-amazon-ebs-volumes-for-data-intensive-workloads/>).

### 2.1 THE FINDING THAT DECIDES THIS QUESTION

> "Volumes that are attached to tasks that are **managed by a service** aren't preserved and are
> **always deleted upon task termination**."
> — <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ebs-volumes.html> (Considerations)

Confirmed three independent ways:

1. `ServiceManagedEBSVolumeConfiguration` **has no `terminationPolicy` field at all**
   (<https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ServiceManagedEBSVolumeConfiguration.html>),
   whereas `TaskManagedEBSVolumeConfiguration` does
   (<https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_TaskManagedEBSVolumeConfiguration.html>).
2. The deployment doc's JSON comment: `"deleteOnTermination": true //can't be configured for
   service-managed tasks, always true`
   (<https://docs.aws.amazon.com/AmazonECS/latest/developerguide/configure-ebs-volume.html>).
3. The storage-options table: EBS is *"Can be persisted when attached to a standalone task.
   **Ephemeral when attached to a task maintained by a service.**"*
   (<https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_data_volumes.html>)

**Consequence for Lightbulb**: the platform runs one always-on **ECS service** per owner
(`openclaw-{user_id}-{hash}`). Under that model an ECS-managed EBS volume is **exactly as ephemeral
as ephemeral storage**, but costs $0.08/GB-month extra and adds volume create/attach time to every
task start. It buys nothing unless the service model is abandoned for `RunTask`.

And even then: **"You can attach at most one Amazon EBS volume to each Amazon ECS task, and it must
be a new volume. You can't attach an existing Amazon EBS volume to a task."** A preserved volume can
never be re-attached — you must snapshot it and create a new volume from the snapshot. There is **no
"snapshot on task stop"** feature; `deleteOnTermination: false` merely leaks the volume for you to
snapshot yourself.

### 2.2 Supported configuration

| Item | Value |
|---|---|
| Fargate platform version | **1.4.0 or later**, Linux only. Windows on Fargate: **not supported** |
| Volume types | `gp2`, `gp3`, `io1`, `io2`, `st1`, `sc1`. **`standard` (magnetic) not supported on Fargate** |
| Sizes | gp2/gp3 **1–16,384 GiB**; io1/io2 **4–16,384**; st1/sc1 **125–16,384** |
| IOPS | gp3 **3,000–16,000** (default 3,000); io1 **100–64,000**; io2 **100–256,000**. Not settable for st1/sc1 |
| Throughput | gp3 only, up to **1,000 MiB/s** |
| Filesystems | `ext3`, `ext4`, `xfs` (default `xfs`), `ntfs` on Windows/EC2. From a snapshot the fs type **must match** or the task fails to start |
| Deployment controller | Must be **`ECS`** (rolling or blue/green). CODE_DEPLOY / EXTERNAL not supported |
| AZ | EBS volumes are **AZ-scoped**; ECS creates the volume in the task's AZ. **Cannot be used in `use1-az3`** for Fargate |
| Snapshots | `snapshotId` supported; **snapshots containing partitions are not supported** |
| Outposts | Not supported |
| IAM | Requires an ECS **infrastructure IAM role** (`AmazonECSInfrastructureRolePolicyForVolumes`) |
| Tags | `AmazonECSCreated` + `AmazonECSManaged` are reserved; 48 tags left to you |
| EFS coexistence | **No documented prohibition.** The storage-options table lists EBS and EFS as independent volume types; the EBS Considerations list does not exclude EFS. **[unverified — I found no AWS statement either allowing or forbidding both in one task definition]** |
| Fargate Spot | **No documented statement either way.** The launch post and docs say "Fargate and EC2" without qualifying by capacity provider. **[unverified]** |

### 2.3 The 1 vCPU problem

<https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ebs-fargate-performance-limits.html>

> "If you request **0.25, 0.5, or 1 virtual CPU unit (vCPU)** for your Fargate task, we recommend
> that you configure a General Purpose SSD volume (gp2 or gp3) or an HDD volume (st1 or sc1). If you
> request **more than 1 vCPU**, the following baseline performance limits apply…"

| vCPU | Baseline EBS IOPS (16 KiB) | Baseline throughput (MiB/s, 128 KiB) | Baseline bandwidth (Mbps) |
|---|---|---|---|
| 2 | 3,000 | 75 | 360 |
| 4 | 5,000 | 120 | 1,150 |
| 8 | 10,000 | 250 | 2,300 |
| 16 | 15,000 | 500 | 4,500 |

> "When you configure an Amazon EBS volume for attachment to a Fargate task, the Amazon EBS
> performance limit for Fargate task is **shared between the task's ephemeral storage and the
> attached volume**."

Two things follow for a **1 vCPU** box: (a) AWS publishes **no** baseline for ≤1 vCPU — it is below
the 2-vCPU row's 3,000 IOPS / 75 MiB/s, so gp3's free 3,000 IOPS / 125 MiB/s is unreachable anyway;
(b) an attached volume and ephemeral storage **split the same budget**, so adding EBS to a task that
already uses ephemeral storage does not add I/O headroom, it divides it.

### 2.4 Create / attach / restore time

- "Volume creation adds a few seconds to task startup time." **[third-party; no AWS-published number found]**
- Snapshot restore was best-effort until **2025-05**, when ECS added `volumeInitializationRate`
  (<https://aws.amazon.com/about-aws/whats-new/2025/05/amazon-ecs-ebs-provisioned-rate-volume-initialization/>).
  Valid range **100–300 MiB/s**. AWS's worked example: a **20 GiB volume created from a snapshot
  holding 10 GiB of data at 300 MiB/s is fully initialized in ~34.1 seconds.**
- Without it, a fresh-from-snapshot volume serves I/O at degraded latency until blocks are pulled
  from S3 (<https://aws.amazon.com/blogs/storage/addressing-i-o-latency-when-restoring-amazon-ebs-volumes-from-ebs-snapshots/>).

### 2.5 Price

gp3 **$0.08/GB-month**; EBS snapshots (standard tier) **$0.05/GB-month**; snapshot archive
**$0.0125/GB-month** with a 90-day minimum; Fast Snapshot Restore **$0.75 per DSU-hour**
(<https://aws.amazon.com/ebs/pricing/> — the FSR number is quoted directly on the page; the gp3 and
snapshot rates are from the page's worked examples and are widely corroborated, **treat the
gp3/snapshot rates as high-confidence but re-check the live pricing table before committing budget**).

---

## 3. Amazon EFS

Source: <https://docs.aws.amazon.com/efs/latest/ug/performance.html> and
<https://docs.aws.amazon.com/efs/latest/ug/performance-tips.html>

### 3.1 Latency — identical across throughput modes

| FS type | Throughput mode | Read latency | Write latency | Max read IOPS | Max write IOPS | Per-client read/write |
|---|---|---|---|---|---|---|
| Regional | **Elastic** | ~1 ms | ~2.7 ms | 900,000–2,500,000 | 500,000 | **1,500 MiB/s** (client v2.0+) |
| Regional | Provisioned | ~1 ms | ~2.7 ms | 55,000 | 25,000 | 500 MiB/s |
| Regional | **Bursting** | ~1 ms | ~2.7 ms | 35,000 | 7,000 | 500 MiB/s |
| One Zone | any | ~1 ms | ~1.6 ms | 35,000 | 7,000 | 500 MiB/s |

Footnote: with Elastic, default caps are **90,000 read IOPS for infrequently accessed data, 250,000
read IOPS for frequently accessed data, 50,000 write IOPS**, each raisable ~10× by quota request.
"Latency values shown represent best-case performance under optimal conditions."

**The decisive point: read latency is ~1 ms in every mode.** Moving from Bursting to Elastic buys
throughput and IOPS ceiling, **not** per-operation latency. The 150 s walk stays ~150 s.

Max I/O performance mode is explicitly *worse*: "Max I/O performance mode has higher per-operation
latencies than General Purpose performance mode." (The deployment is already on General Purpose —
correct.)

### 3.2 IOPS accounting

> "You are not billed separately for IOPS; they are included in a file system's throughput
> accounting. **Every NFS request is accounted for as 4 kilobyte (KB) of throughput, or its actual
> request and response size, whichever is larger.**"

So 114,000 `getattr`/`lookup` calls = 114,000 × 4 KiB ≈ **456 MiB of metered "throughput"** for a walk
that transfers almost no data. That is why `PercentIOLimit` sat under 4% while the walk crawled —
the IOPS ceiling was never the binding constraint.

### 3.3 Elastic-mode per-operation cost

<https://aws.amazon.com/efs/faq/>:

> "EFS data transfer charges for metadata operations such as list (ls), remove (rm), and create
> directory (mkdir)… For Elastic Throughput, **metadata operations are metered at a minimum of
> 4 KiB**." Worked examples on the page: `mkdir` = 4 KiB; listing a directory of 10 files =
> `10 × 4 KiB = 40 KiB`.

Rates (us-east-1): Standard storage **$0.30/GB-month**; Elastic throughput **$0.03/GB read**,
**$0.06/GB write** (<https://aws.amazon.com/efs/pricing/> — the live table is rendered client-side and
did not extract; these rates are corroborated by AWS's own FAQ worked examples and multiple
secondary sources. **Treat as high-confidence, re-verify before budgeting.**).

**Concrete**: one 114k-entry walk in Elastic mode ≈ 456 MiB read ≈ 0.445 GB × $0.03 = **$0.013 per
walk**. Trivial once; at 1,000 workspaces × 20 walks/day that is **$260/month of pure `ls`**.
Bursting mode has no such per-op charge — so switching to Elastic to "fix" this would *add* cost and
*not* fix it.

### 3.4 Bursting reality check for a 28 GB filesystem

Bursting baseline = **50 KiB/s per GiB** stored in Standard. At 28 GB that is ~1.4 MiB/s baseline
(AWS floors all file systems at 1 MiB/s metered), bursting to **100 MiB/s minimum**. Credit cap is
2.1 TiB for file systems under 1 TiB. A 28 GB file system is at the very bottom of the Bursting
curve — it survives only because the workload is metadata-bound, not byte-bound.

### 3.5 AWS's explicit small-file / directory guidance

From `performance-tips.html`, verbatim:

- "Don't unnecessarily close files if you will need them later in a workflow… File open, close, and
  metadata operations generally cannot be made asynchronously or through a pipeline."
- "Use parallelism to reduce the impact of round-trip times."
- "Bundle reference files in a `.zip` file… allows you to read many files with one open-close round trip."
- "When performing a listing (**ls**) on very large directories (**over 100k files**) that are being
  modified concurrently, Linux NFS clients can hang, not returning a response."
- "We recommend keeping the number of directories on your file system to **less than 10,000**, if possible."
- "When listing a directory, **avoid getting file attributes if they are not required**, because they
  are not stored in the directory itself."
- "**NFSv4.1 provides better performance for parallel small-file read operations (greater than 10,000
  files per second) compared to NFSv4.0 (less than 1,000 files per second).**"
- Never use `noac`, `actimeo=0`, `acregmax=0`, `acdirmax=0`, `lookupcache=pos`, `lookupcache=none` —
  "very large performance impact."
- To hit max IOPS: "Distribute the workload across **10 or more NFS clients, with at least 64 threads
  per client**."

AWS never names git or `node_modules` in the docs, but the 100k-directory hang warning and the
"minimize file reopens / bundle into a zip" advice describe a git tree precisely. (Secondary sources
are blunter — NetApp's EFS do's-and-don'ts says running git repos on EFS "is not efficient because it
requires very frequent access to a large number of small files": <https://www.netapp.com/blog/efs-performance-dos-and-donts-aws-cvo-blg/>.)

**Actionable within EFS**: confirm the mount is NFSv4.1 (10k files/s vs <1k for 4.0), confirm the
attribute cache is on, and **parallelise the walk** (10+ threads ≈ 10× on a 1.3 ms-per-op wall).
`nconnect` is supported on EFS via amazon-efs-utils; it is **not** supported on S3 Files (§4.2).

---

## 4. S3-backed patterns for a working tree

### 4.1 Checkpoint sync (`aws s3 sync` / tarball to S3, restore at boot)

This is the pattern the platform **already runs for SQLite** (restore from S3 at boot, litestream
replicate). Extending it to the working tree needs no new AWS service.

Costs (us-east-1, S3 Standard): storage **$0.023/GB-month**; **PUT/COPY/POST/LIST $0.005 per 1,000
requests**; **GET $0.0004 per 1,000**.

- Syncing a 114,000-file tree **file-by-file**: 114,000 PUTs = **$0.57 per full sync**, plus 114k
  round trips of latency. At 1,000 workspaces syncing hourly this is absurd.
- Syncing it as **one tarball**: 1 multipart upload, ~$0.0001, one sequential stream. Restore = one
  GET + `tar -x` onto local disk.
- **Therefore: checkpoint as an archive, never as a file-by-file sync.** Git already has the right
  primitive — `git bundle` (or just `tar` of `.git` + a dirty-file list).
- `aws s3 sync` semantics (size + mtime comparison, `--delete`, `--exact-timestamps`):
  <https://docs.aws.amazon.com/cli/latest/reference/s3/sync.html>

Failure modes: an uncheckpointed crash loses work since the last checkpoint; two writers (a
transiently-doubled task during a `maxHealthyPercent: 200` rolling deploy) can both restore-and-write
and the last checkpoint wins. That second one is real for this platform — the backend service already
runs at 200% during deploys, and per-owner containers get replaced on every image roll.

### 4.2 Mountpoint for S3 — **not usable for a git working tree**

<https://docs.aws.amazon.com/AmazonS3/latest/userguide/mountpoint.html>

> "Mountpoint supports basic file system operations, and can read files up to 50 TB in size. It can
> list and read existing files, and it can create new ones. **It cannot modify existing files or
> delete directories, and it does not support symbolic links or file locking.** Mountpoint is ideal
> for applications that do not need all of the features of a shared file system and POSIX-style
> permissions… For workloads that require full POSIX support, we recommend Amazon FSx for Lustre."

Mountpoint's own semantics doc adds that **rename is not supported at all** on general purpose
buckets (<https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md>). Git's entire object
and index machinery is write-temp-then-rename, plus `index.lock` file locking, plus in-place edits.
Mountpoint fails on all three. AWS's own guidance in the Mountpoint docs is explicit that you should
not work on a Git repository in a Mountpoint mount. **Dead end — do not evaluate further.**

### 4.3 **Amazon S3 Files** — the option that did not exist when EFS was chosen

GA **2026-04-07**. A managed **NFSv4.1/4.2** file system whose backing store is an S3 bucket, with a
low-latency "high-performance storage" tier in front of it. **Supported as a first-class ECS task
volume on Fargate** via `s3filesVolumeConfiguration`
(<https://docs.aws.amazon.com/AmazonECS/latest/developerguide/s3files-volumes.html>) — not supported
on the EC2 launch type yet.

Performance (<https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-performance.html>):

| Spec | Value |
|---|---|
| Aggregate read throughput per FS | "Up to terabytes per second" |
| Aggregate write throughput per FS | 1–5 GiB/s |
| Max read IOPS per FS | 250,000 |
| Max write IOPS per FS | 50,000 |
| Max per-client read throughput | 3 GiB/s |
| Import from bucket | 2,400 objects/s, 700 MB/s |
| Export to bucket | 800 files/s, 2,700 MB/s |

> "The first time you access a directory, S3 Files imports metadata for all files in that directory…
> So your initial access takes longer than subsequent operations. **Once imported, all subsequent
> directory listings and file access return at low latency.**"
> "S3 Files asynchronously imports data for small files (< 128 KiB by default) to the file system's
> high-performance storage for low latency access on subsequent reads."

**AWS publishes no per-operation latency number for S3 Files.** Third-party coverage claims
"sub-millisecond latency" (<https://www.hpcwire.com/2026/04/09/aws-delivers-high-performance-nfs-access-with-s3-files/>)
— **[unverified]**. It is still NFS over the network, so a serialized 114k-entry walk still pays
114k round trips. **Whether an S3 Files walk of the same tree is materially faster than 150 s is
UNPROVEN and must be benchmarked before it is designed in.**

Semantics/limits (<https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-quotas.html>):
- **No hard links** (git does not need them for a normal clone; `git clone --local` and some `git
  worktree` internals may — verify).
- **`nconnect` mount option NOT supported** — so you cannot buy parallelism with more TCP connections
  the way you can on EFS.
- All locks are advisory; no mandatory locking, no Kerberos, no pNFS, no ACLs.
- Max file 48 TiB; directory depth 1,000; filename 255 bytes; **25,000 connections per FS**;
  **1 mount target per AZ per FS**; **1 VPC per FS**; 25,000 access points per FS; **1,000 file
  systems per account** (raisable).
- Directory rename is a copy+delete of every object under the prefix: "A directory rename of 100,000
  files takes a few minutes to fully reflect in the S3 bucket, though **the rename is instant on the
  file system**."
- Writes batch for ~60 s before export to S3, so a crash within that window can lose the last minute
  of bucket-side state (the file system copy is durable immediately).

Metering (<https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-metering.html>): **metadata
reads and writes are metered at a 4 KiB minimum**, file reads/writes at a **32 KiB minimum**, each
rounded up to the next 1 KiB. High-performance storage has a **10 KiB minimum billable file size**
and data expires from it after a configurable **1–365 days (default 30)**. Rates **[reported, not
extracted from the live table — verify]**: high-performance storage **$0.30/GB-month**, reads
**$0.03/GB**, writes **$0.06/GB**, with the cold bulk staying at S3 Standard $0.023/GB-month.

**The economics are the real win**: the same $0.30/GB-month as EFS, but charged only on the *active
working set*, with everything else at $0.023. For a fleet where most of 28 GB is cold, that is close
to a 10× storage saving. The latency win is unproven.

### 4.4 Litestream-style continuous file replication

Litestream replicates **SQLite WAL frames**, not arbitrary files (<https://litestream.io/reference/replicate/>).
There is no litestream-for-a-directory. The generic equivalents are a periodic archive+PUT (§4.1) or
`restic`/`kopia` style content-addressed incremental backup to S3 — both are checkpoint models with a
recovery-point-objective equal to the checkpoint interval, not continuous replication. **Nothing
AWS-native offers continuous file-level replication of a POSIX tree to S3 except S3 Files (§4.3),
which is exactly that with a ~60 s export batching window.**

---

## 5. Amazon Bedrock AgentCore Runtime (2025/2026)

Docs: <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html>,
<https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html>,
<https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html>

**Isolation model**: "Each user session in AgentCore Runtime receives its own dedicated **microVM**
with isolated Compute, memory, and filesystem resources… After session completion, the entire microVM
is terminated and memory is sanitized."

**Lifetime**: idle timeout default **15 minutes** (`idleRuntimeSessionTimeout`); max compute lifetime
default **8 hours** (`maxLifetime`, adjustable); "Sessions last up to 8 hours for each lifecycle on
microVMs, **or up to 14 days on Instances**." A stopped session **resumes** on the next invocation
with a new microVM and a fresh 8-hour clock; "The session itself remains valid until the AgentCore
Runtime ARN is deleted."

**Persistence — three options, and the caps matter:**

| Type | Isolation | Persistence | Cap |
|---|---|---|---|
| **Managed session storage** (Preview, microVM) | per-session | survives stop/resume; **reset after 14 days idle**; **wiped on every runtime version update** | **1 GB total, ~50 MB metadata ≈ 100,000–200,000 files**, depth 200 |
| **Capacity provider volume** (Instances, = EBS in your account) | per-session | survives stop/resume and max-lifetime; deleted only when you delete the session or capacity provider | EBS sizes; 5 volumes |
| **BYO EFS / S3 Files access point** (microVM) | shared across sessions/agents | customer-managed | requires VPC mode |

AWS's own doc holds this up as the coding-agent pattern — *"Coding agent with persistent project
files (microVM) → Managed session storage at `/mnt/workspace`"*, and says "`ls`, `cat`, `mkdir`,
**`git`**, `npm`, `pip`, and `cargo` all work as expected", with "source files, installed packages,
build artifacts, and **.git history** are all intact" after resume.

**Why it does not fit Lightbulb as-is:**
1. **1 GB session storage cap.** A git clone plus 13 worktrees is not going to fit; the ~100k–200k
   file metadata ceiling is also right at the 114k the incident hit.
2. **Data is wiped on every runtime version update.** Lightbulb ships a new OpenClaw image regularly;
   each roll would erase every customer's workspace.
3. **Max hardware per session is 2 vCPU / 8 GB**, no larger.
4. **Session ≠ tenant.** AgentCore "does not enforce session-to-user mappings — your client backend
   should maintain the relationship." A per-customer always-on agent maps to a per-customer *session*
   that never truly ends, which is fighting the model.
5. It would replace the entire ECS/gateway/Cloud-Map runtime, not just storage.

**AgentCore Runtime Instances** (announced **2026-08-06**,
<https://aws.amazon.com/blogs/aws/runtime-instances-persistent-compute-for-production-ai-agents-on-amazon-bedrock-agentcore/>)
is the closer fit — AWS-managed EC2, **sessions up to 14 days**, EBS capacity-provider volumes that
persist across stop/resume and are deleted only on session delete, up to 20 agents per session. But
it is EC2 underneath, priced as **EC2 On-Demand + a 12% management fee (7.8% GPU)**, billed per
instance-hour from boot, ~1-minute minimum. That is a different, more expensive shape than 1 vCPU
Fargate Spot.

**Pricing** (<https://aws.amazon.com/bedrock/agentcore/pricing/>): Runtime microVMs **$0.0895 per
vCPU-hour** and **$0.00945 per GB-hour**, per-second with a 1-second minimum, **CPU billed only
during active use** ("if your agent consumes no CPU during I/O wait, there are no CPU charges").
Session-storage pricing is **not yet announced** (preview). Note the vCPU rate is **~2.2× Fargate
on-demand** — AgentCore is cheap only because idle costs nothing, which is the opposite of an
always-on per-customer container.

**Other quotas**: 5,000 active session workloads per account in us-east-1 / us-west-2 (2,500
elsewhere, raisable); 1,000 agents per account; 2 GB max container image; 25 TPS new-session creation;
15-minute sync request timeout; 100 MB payload.

---

## 6. Firecracker snapshot/restore on AWS

- Firecracker requires **KVM**: "setting up a development environment requires either a bare-metal
  machine (with hardware virtualization), or a virtual machine that supports nested virtualization"
  (<https://github.com/firecracker-microvm/firecracker/blob/main/docs/dev-machine-setup.md>).
- Snapshot/restore is a first-class Firecracker feature — serialize a running microVM and its
  resources, restore later in a different Firecracker process, guest workload simply resumes
  (<https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md>).
- **The bare-metal-only constraint is GONE as of 2026.** EC2 added **nested virtualization on virtual
  instances** effective **2026-02-16** (C8i, M8i, R8i;
  <https://aws.amazon.com/about-aws/whats-new/2026/02/amazon-ec2-nested-virtualization-on-virtual/>),
  expanded **2026-06** to C7i, R7i, M7i, C8id, R8id, M8id, C7i-flex, M7i-flex, I7i, C8i-flex,
  R8i-flex, M8i-flex, X8i and GovCloud
  (<https://aws.amazon.com/about-aws/whats-new/2026/06/nested-virtualization-intel-us-gov-cloud/>).
  Docs: <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/amazon-ec2-nested-virtualization.html>.
- **There is no managed Firecracker service you can hand a snapshot to.** Firecracker is the
  *implementation* under Lambda, Fargate and AgentCore microVMs, but none of them exposes
  snapshot/restore as an API. (Lambda SnapStart is the nearest managed analogue and is Java/.NET/
  Python runtime-level, not a general filesystem snapshot.)
- **Verdict**: self-hosting Firecracker means owning an EC2 fleet, a snapshot store, a scheduler, a
  placement layer and a security boundary — i.e. rebuilding Fargate. Not proportionate to "a directory
  walk is slow". Rule it out.

---

## 7. Briefly: Lambda + EFS, App Runner

- **Lambda + EFS**: real and supported (<https://aws.amazon.com/blogs/compute/using-amazon-efs-for-aws-lambda-in-your-serverless-applications/>),
  but Lambda has a **15-minute max execution** and `/tmp` ephemeral storage of 512 MB–**10 GB**
  (<https://aws.amazon.com/blogs/aws/aws-lambda-now-supports-up-to-10-gb-ephemeral-storage>). EFS
  supports **25,000 connections per file system**, and each concurrent Lambda holds one for the whole
  invocation — so concurrency must stay under that. It mounts the *same* EFS with the *same* ~1 ms
  per-op latency, so it solves nothing here. Irrelevant to an always-on agent.
- **App Runner**: storage is ephemeral, **no EFS mount** — the roadmap request has been open since
  2021 and unimplemented (<https://github.com/aws/apprunner-roadmap/issues/14>). Worse, the AWS docs
  now carry an availability notice that **App Runner is no longer open to new customers** (existing
  customers continue). Rule it out.

---

## 8. Comparison table

Cost columns = **storage only, per month, us-east-1, 20 GB of working tree per workspace**. Compute
is identical across rows and excluded. `(100)` = 100 workspaces, `(1,000)` = 1,000.

| Option | Durability of the tree | Restore time after task replacement | Cost @20 GB ×100 | ×1,000 | Blast radius | Ops burden |
|---|---|---|---|---|---|---|
| **Fargate ephemeral storage alone** | **None** — gone on every task stop | n/a (tree is lost) | **$0** (20 GiB included; image eats into it) | **$0** | One task | ~0 |
| **Ephemeral + S3 archive checkpoint/restore** *(recommended)* | Durable to last checkpoint (RPO = checkpoint interval) | tarball GET + untar onto local disk: **~10–60 s for a 0.2–2 GB tree**; ~7–9 min for a true 20 GB tree at ≤360 Mbps **[bandwidth for 1 vCPU unpublished]** | **$46** (S3 Std $0.023/GB) + ~$0.001 PUTs | **$460** | One tenant's prefix | Low — same shape as the existing litestream/S3 SQLite boot path |
| **ECS-managed EBS on a *service*** | **None** — "always deleted upon task termination" | n/a | **$160** (gp3 $0.08/GB) | **$1,600** | One task | Medium — infra IAM role, `configuredAtLaunch`, AZ pinning — **for zero durability** |
| **ECS-managed EBS on a *standalone task* + snapshot** | Durable if you snapshot before/after stop | create-from-snapshot at 100–300 MiB/s: **~34 s for 10 GiB of data**, + a few s to attach | $160 + snapshots ~$100 (@$0.05/GB, full) | $1,600 + ~$1,000 | One tenant's volume | **High** — you lose ECS service self-healing, must orchestrate snapshot-on-stop yourself, AZ-pinned, `use1-az3` excluded |
| **EFS (today)** | Durable, multi-AZ, 11 9s | 0 (always mounted) | **$600** ($0.30/GB) + Elastic read/write if switched | **$6,000** | **Fleet-wide** — one file system, shared IOPS/throughput ceiling, one noisy tenant degrades all | Low (already built) — but the 1.3 ms/op wall is structural |
| **Amazon S3 Files** | Durable in S3; ~60 s export batching window | 0 mounted; **first access to a directory imports its metadata** (slower once) | ~**$100** (≈2 GB hot @$0.30 + 20 GB cold @$0.023) **[assumes 10% hot]** | ~**$1,000** | Per file system; shardable (1,000 FS/account, 1 VPC/FS, 1 mount target per AZ) | Medium — new service, ECS volume type exists on Fargate, **latency benefit unproven** |
| **AgentCore Runtime session storage** | Survives stop/resume; **wiped on runtime version update**, reset after 14 days idle | Service-managed, unpublished | n/a — **1 GB cap makes 20 GB impossible** | n/a | Per session | **High** — replaces the whole runtime |
| **AgentCore Runtime Instances + capacity-provider EBS** | Persists until session deleted, 14-day sessions | EBS re-attach on resume | EC2 + **12% management fee**, not comparable per-GB | — | Per session/instance | **High** — full runtime migration |
| **Self-hosted Firecracker + snapshots** | Whatever you build | Sub-second microVM restore in theory | EC2 fleet cost | — | Whatever you build | **Very high** — you are rebuilding Fargate |
| **Mountpoint for S3** | — | — | — | — | — | **Disqualified**: no rename, no in-place modify, no file locking — git cannot run on it |

---

## 9. Recommendation for a 1 vCPU always-on per-customer agent container

### Primary: **move the working tree to Fargate ephemeral storage; keep durability in S3 as archives; leave EFS for small config/state.**

Rationale, in order of weight:

1. **Nothing else removes the 1.3 ms round trip.** EFS latency is ~1 ms in *every* throughput mode
   (§3.1), S3 Files latency is unpublished (§4.3), and a local block device is ~1000× faster. Only
   local storage is a categorical fix.
2. **It is free.** 20 GiB of ephemeral storage is included in every Fargate task; the container image
   is the only competitor for that space. Going to 100 GiB costs 80 × $0.081 = **$6.48/workspace-month**.
3. **The pattern already exists in this codebase.** SQLite already restores from S3 at boot and
   replicates continuously. The working tree becomes the second thing that boot script rehydrates.
   No new AWS service, no new IAM surface, no new failure mode class.
4. **EBS adds nothing under the ECS-service model.** Service-managed volumes are "always deleted upon
   task termination" (§2.1) — same durability as ephemeral storage, +$0.08/GB-month, + attach time,
   + an infrastructure IAM role, and on a 1 vCPU task it *shares* the I/O budget with ephemeral
   storage rather than adding to it (§2.3).

Implementation shape (the lazy version):
- Working tree at `/workspace` on ephemeral storage. Bump `ephemeralStorage.sizeInGiB` only when the
  image + tree genuinely exceed 20 GiB.
- Checkpoint = **one archive object per owner**, not a file-by-file `s3 sync` (114k PUTs = $0.57 and
  114k round trips; one tarball = one multipart upload).
- Trigger checkpoints on: idle-park, SIGTERM during task drain, and a coarse timer. RPO = timer.
- Restore at boot: one GET + untar, before the agent accepts work.
- **Keep `openclaw.json`, the lock sidecars and small per-owner state on EFS.** They are small,
  latency-tolerant, must survive a task dying mid-checkpoint, and the existing `.isol8-guard` /
  `.isol8-protocol` lock protocol depends on EFS semantics. Do not migrate those.
- Two writers during a `maxHealthyPercent: 200` roll are the real hazard: last checkpoint wins. Gate
  checkpoint-write on the same claim-lease pattern already used for provisioning.

**Before doing any of this, check whether the 150 s walk needs to happen at all.** The incident was an
EFS tree walk *on the event loop*; 114k entries is also right at the threshold where AWS warns Linux
NFS clients can hang outright on `ls` (§3.5). Not walking, walking with `--no-attributes`, or walking
across 10+ threads may be a one-line fix that makes the whole storage migration unnecessary. Storage
architecture is the expensive answer to a question that may have a cheap one.

### Fallback: **Amazon S3 Files replacing EFS for the workspace mount.**

Take this if the tree genuinely must be a shared, always-present, durable POSIX mount and cannot be
rehydrated at boot.

- It is a supported **Fargate task volume type** today (`s3filesVolumeConfiguration`), GA 2026-04-07.
- It keeps full POSIX rename/lock semantics that Mountpoint lacks, so git works.
- It should be **~6× cheaper than EFS** at this fleet's hot/cold ratio, because $0.30/GB-month applies
  only to the active working set and the rest sits at S3 Standard $0.023.
- **Gate it on a benchmark**: mount the same 114k-entry tree and time the same walk. AWS publishes no
  per-op latency for S3 Files, `nconnect` is unsupported so you cannot buy parallelism with
  connections, and first-access-per-directory pays a metadata import. If the walk is not
  *categorically* faster (not 20% — 5×+), S3 Files is a cost optimisation, not a latency fix, and the
  primary recommendation still stands.

### Explicitly rejected

- **Mountpoint for S3** — no rename, no in-place modify, no locking. Git cannot run on it. AWS says so.
- **ECS-managed EBS on a service** — zero durability by design, plus cost.
- **AgentCore Runtime (microVM)** — 1 GB session storage, wiped on every runtime version update, 2
  vCPU / 8 GB ceiling. Fatal on all three counts for a 20 GB persistent per-customer workspace.
- **Self-hosted Firecracker** — nested virtualization on EC2 has made it *possible* since 2026-02, and
  it is still rebuilding Fargate to fix a `readdir` loop.
- **App Runner** — no persistent storage, and closed to new customers.
- **Lambda + EFS** — 15-minute ceiling, same EFS latency. Not an always-on agent.

---

## 10. Things to verify before committing

1. **Live pricing table values** for EBS gp3 / snapshots, EFS Standard + Elastic read/write, and S3
   Files. All three pricing pages render client-side; the numbers above come from AWS worked examples
   and corroborating sources, not from the tables themselves.
2. **Fargate Spot + ECS-managed EBS** — no AWS statement found either way.
3. **EBS + EFS volumes in the same task definition** — no AWS statement found either way.
4. **Per-task baseline EBS IOPS/throughput/bandwidth at ≤1 vCPU** — unpublished; the table starts at 2 vCPU.
5. **S3 Files per-operation latency and a 114k-entry walk benchmark** — the single most decision-relevant
   unknown in this document.
6. **Whether Fargate ephemeral storage is EBS-backed or instance-store NVMe** — the shared-performance-limit
   sentence implies EBS; `CLAUDE.md` currently says NVMe. Worth settling for capacity planning.
7. **Whether `git worktree` needs hard links** on the chosen filesystem (S3 Files has none).

---

## Sources

- <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-task-storage.html>
- <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-storage-encryption.html>
- <https://aws.amazon.com/fargate/pricing/>
- <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ebs-volumes.html>
- <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/configure-ebs-volume.html>
- <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ebs-fargate-performance-limits.html>
- <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/using_data_volumes.html>
- <https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_TaskManagedEBSVolumeConfiguration.html>
- <https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_ServiceManagedEBSVolumeConfiguration.html>
- <https://aws.amazon.com/about-aws/whats-new/2024/01/amazon-ecs-fargate-integrate-ebs/>
- <https://aws.amazon.com/blogs/aws/amazon-ecs-supports-a-native-integration-with-amazon-ebs-volumes-for-data-intensive-workloads/>
- <https://aws.amazon.com/about-aws/whats-new/2025/05/amazon-ecs-ebs-provisioned-rate-volume-initialization/>
- <https://aws.amazon.com/blogs/storage/addressing-i-o-latency-when-restoring-amazon-ebs-volumes-from-ebs-snapshots/>
- <https://aws.amazon.com/ebs/pricing/>
- <https://docs.aws.amazon.com/efs/latest/ug/performance.html>
- <https://docs.aws.amazon.com/efs/latest/ug/performance-tips.html>
- <https://aws.amazon.com/efs/faq/>
- <https://aws.amazon.com/efs/pricing/>
- <https://docs.aws.amazon.com/AmazonS3/latest/userguide/mountpoint.html>
- <https://github.com/awslabs/mountpoint-s3/blob/main/doc/SEMANTICS.md>
- <https://docs.aws.amazon.com/AmazonECS/latest/developerguide/s3files-volumes.html>
- <https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-performance.html>
- <https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-metering.html>
- <https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-quotas.html>
- <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html>
- <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html>
- <https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html>
- <https://aws.amazon.com/blogs/aws/runtime-instances-persistent-compute-for-production-ai-agents-on-amazon-bedrock-agentcore/> (2026-08-06)
- <https://aws.amazon.com/bedrock/agentcore/pricing/>
- <https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md>
- <https://github.com/firecracker-microvm/firecracker/blob/main/docs/dev-machine-setup.md>
- <https://aws.amazon.com/about-aws/whats-new/2026/02/amazon-ec2-nested-virtualization-on-virtual/> (2026-02-16)
- <https://aws.amazon.com/about-aws/whats-new/2026/06/nested-virtualization-intel-us-gov-cloud/>
- <https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/amazon-ec2-nested-virtualization.html>
- <https://aws.amazon.com/blogs/compute/using-amazon-efs-for-aws-lambda-in-your-serverless-applications/>
- <https://aws.amazon.com/blogs/aws/aws-lambda-now-supports-up-to-10-gb-ephemeral-storage>
- <https://github.com/aws/apprunner-roadmap/issues/14>
- <https://www.netapp.com/blog/efs-performance-dos-and-donts-aws-cvo-blg/> (third-party)
- <https://www.hpcwire.com/2026/04/09/aws-delivers-high-performance-nfs-access-with-s3-files/> (third-party)
