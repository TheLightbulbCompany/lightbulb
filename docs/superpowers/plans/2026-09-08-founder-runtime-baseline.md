# Founder OpenClaw runtime baseline — 2026-09-08

Scope: bounded, read-only before-state for the authorized founder-only OpenClaw upgrade candidate `org_3H3viGAfq2SXc9dIdcatmGXg8Td`. No AWS state was changed. No task was invoked, restarted, deployed into, or accessed with ECS Exec. No log stream, transcript, customer message, gateway token, secret value, task-definition environment value, or runtime configuration content was fetched.

## Identity and target fence

- Every AWS command explicitly used `--profile isol8-admin --region us-east-1`.
- STS was checked first and returned account `877352799272` with the SSO `AdministratorAccess` assumed role for `Prasiddha`. The baseline would have stopped on an account mismatch.
- Production cluster: `lightbulb-prod-container` (`ACTIVE`).
- Candidate owner: `org_3H3viGAfq2SXc9dIdcatmGXg8Td`, typed `org` in the strongly consistent container-row read.
- Exact service: `openclaw-org_3H3viGAfq2SXc9dIdcatmGXg8Td-c42489f74b24`.

This establishes an exact DynamoDB/ECS mapping for the supplied candidate. It does not independently re-prove the human founder's current Clerk organization selection; reconfirm that identity immediately before any mutation.

## Current runtime

Observed at approximately `2026-09-08T21:50Z`:

| Property | Before-state |
|---|---|
| DynamoDB table/key | `lightbulb-prod-containers`, `owner_id=org_3H3viGAfq2SXc9dIdcatmGXg8Td` |
| Metadata state | `status=running`, `substatus=gateway_healthy`; last handshake `2026-09-08T21:25:55.813244Z` |
| ECS service | `ACTIVE`, desired/running/pending `1/1/0`; one `PRIMARY` deployment, rollout `COMPLETED`, failed tasks `0` |
| Task definition | `lightbulb-prod-openclaw-user:206`, `ACTIVE`, registered `2026-09-08T17:16:52.184Z` |
| Actual running task | `7dcd7cea19b044a3a0f4bf35823234d6`, `RUNNING`, `HEALTHY`, `CONNECTED`; started `2026-09-08T21:04:24.131Z` |
| Compute | Fargate `1.4.0`, `FARGATE_SPOT`, `x86_64`, 1 vCPU (`1024`), 4 GiB (`4096` MiB), 20 GiB task ephemeral storage |
| Image | `877352799272.dkr.ecr.us-east-1.amazonaws.com/lightbulb/openclaw-extended:2026.8.1-isol8.3-4e22eb8` |
| Running digest | `sha256:c23039deb00f5293f3d2a3c905e7621177527e1fe97d0f7a334d564db515ba88` |
| ECR resolution | The tag resolves to the same digest; the digest is also tagged `2026.8.1-isol8.3-fb0b300` and `2026.8.1-isol8.3-ee54a74` |
| Networking | `awsvpc`, private IP only, three configured private subnets, security group `sg-07bafb37c14f059d2` |
| Process/lifecycle | Essential `openclaw` container, TCP `18789`, init process enabled, 120-second stop timeout, `awslogs` driver |

The service has `enableExecuteCommand=true`, but ECS Exec was deliberately not used.

### Stale metadata pointer

The DynamoDB row's `task_arn` still names task `8795101cfe32408ab4712cdb70312a9d`, which is stopped. It was interrupted by Fargate Spot at `2026-09-08T21:02:26Z` (`stopCode=SpotInterruption`, `Your Spot Task was interrupted.`), and its container exited `1`. ECS replaced it with the healthy running task above.

Therefore, the service's `list-tasks --desired-status RUNNING` result, not the row's cached `task_arn`, is the authoritative task locator for the upgrade. The row's retained `stop_reason=admin` is also stale historical metadata and does not describe the observed Spot interruption.

## Storage and recovery wiring

The task definition and source agree on a split layout:

- `openclaw-workspace` mounts encrypted EFS filesystem `fs-07640461cc0f09b14` at `/home/node/.openclaw` through access point `fsap-04076f480853c04fd`, with EFS IAM authorization and transit encryption enabled.
- The access point is `available`, enforces UID/GID `1000`, and is rooted at `/users/org_3H3viGAfq2SXc9dIdcatmGXg8Td` with mode `0755`. Its tags bind it to this owner and `ManagedBy=lightbulb-backend`.
- Two config-less task-local volumes are mounted inside the EFS tree at `/home/node/.openclaw/state` and `/home/node/.openclaw/agents`. These hold the hot SQLite databases on local storage; they disappear with the task.
- The EFS filesystem is `available`, KMS-encrypted, `generalPurpose`/`bursting`, with three mount targets and an `AFTER_30_DAYS` lifecycle transition in source. Whole-filesystem metering, not founder-specific usage, was `25,566,586,880` bytes at `2026-09-08T21:41:24Z` (`17,063,911,424` IA; `8,502,675,456` Standard).
- Litestream `0.5.15` is baked into the image. Source config replicates the main state DB and recursively discovered per-agent DBs to `s3://lightbulb-prod-openclaw-replicas/org_3H3viGAfq2SXc9dIdcatmGXg8Td/...` with a 10-second sync interval. Boot enumerates that prefix, restores each DB only when absent, runs SQLite `quick` integrity checks, and fails closed on enumeration or restore error.
- A one-object, key-suppressed existence check proved the founder replica prefix is non-empty. No object body or object key was returned.
- The founder task role has the expected `owner-litestream-s3` inline policy: bucket location, owner-prefix-limited list, and get/put/delete only under this exact owner prefix.
- The replica bucket uses S3-managed AES-256 encryption and blocks all public access. Bucket versioning is not enabled, and no bucket lifecycle configuration exists; source deliberately delegates replica pruning to Litestream.

Recovery qualification is incomplete: EFS `DescribeBackupPolicy` returned `PolicyNotFound`, and AWS Backup returned zero recovery points for this filesystem. The Litestream replica prefix is a durability mechanism, but this read-only baseline did not perform a restore or establish the age/integrity of the newest replica.

## Narrow operational metrics

Container Insights is disabled on the cluster. Standard `AWS/ECS` service metrics were available without reading logs.

For `2026-09-08T20:50Z`–`21:50:41Z` in five-minute periods, a window that includes the Spot interruption and replacement:

- Latest complete five-minute CPU period (`21:45Z`): average `7.71%`, maximum `18.05%`; maximum one-minute sample in the hour `97.03%` during the replacement/boot window.
- Latest complete five-minute memory period (`21:45Z`): average `38.56%`, maximum `38.75%`; maximum one-minute sample in the hour `50.29%`.

These are service-level metrics, not per-process or per-agent metrics. They are sufficient to establish a lightweight comparison point but not a workload benchmark.

## Source-of-truth cross-check

The inspected Lightbulb source at `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync` confirms:

- DynamoDB uses `owner_id` as the containers-table partition key.
- ECS service names are deterministic: sanitized owner id plus the first 12 hex characters of its SHA-256 hash.
- Per-owner task definitions clone the CDK-managed `lightbulb-prod-openclaw` base into family `lightbulb-prod-openclaw-user`, replacing the EFS access point and preserving per-owner task state.
- `openclaw-version.json` currently pins production to `2026.8.1-isol8.3-4e22eb8`, matching the running task.
- Source defines the EFS/S3/local-SQLite layout described above. No separate IaC AWS Backup plan was found for the OpenClaw EFS filesystem.

## Incomplete requirements before mutation

1. Reconfirm the currently signed-in founder and active Clerk organization match the candidate owner; this baseline proves infrastructure mapping only.
2. Resolve the running task from the ECS service again immediately before the change. Do not reuse the stale DynamoDB `task_arn` or this report's point-in-time task ARN.
3. Decide whether the non-empty Litestream prefix is sufficient rollback protection. There is no EFS automatic backup policy or AWS Backup recovery point, and no restore was tested here.
4. If recovery assurance is required, establish replica freshness and run an isolated, non-production restore/integrity rehearsal under separate authorization. Do not inspect customer content to do a routine version baseline.
5. Recheck service steadiness and image digest immediately before deployment; the service uses interruptible Fargate Spot capacity and already replaced one task during this observation window.
6. Runtime-only facts remain intentionally unknown: live disk free space, database integrity, agent count, boot migration markers, OpenClaw self-reported version, and effective config compatibility. Obtaining them would require runtime access, configuration/content reads, or log inspection outside this task's scope.

## Actions deliberately not taken

- No DynamoDB scan or unprojected container-row read.
- No gateway-token, secret-value, task-definition environment, SSM parameter value, or OpenClaw config read.
- No CloudWatch log or ECS task-event content read.
- No S3 object body read and no customer/message/transcript access.
- No ECS Exec, runtime command, new task, deployment, scale change, restart, backup, snapshot, or restore.
- No AWS configuration, IAM policy, EFS, S3, ECS, ECR, or repository state change.
