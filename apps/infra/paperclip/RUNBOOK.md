# Paperclip Migrations Runbook

After every `cdk deploy` of the Paperclip stack, run the database
migrations one-shot ECS task. This task:

1. Connects to the Paperclip Aurora cluster using the CDK-generated
   master credentials (`lightbulb-{env}-paperclip-db-credentials`,
   auto-injected into the container as `PGPASSWORD`).
2. Creates the `vector` extension if not already present (idempotent —
   safe to re-run).
3. Runs `pnpm --filter @paperclipai/db migrate` from `/app`, which
   applies any new Drizzle migrations.

The task definition is `lightbulb-{env}-paperclip-migrate` (CFN output
`PaperclipMigrateTaskDefArn`). It is a **one-shot task** — there is no
ECS service backing it. Operators must invoke it manually.

## Prerequisites

- AWS SSO logged in: `aws sso login --profile isol8-admin`
- The Paperclip stack has been deployed (`lightbulb-{env}-paperclip`)
- Have these values from CFN outputs (or the AWS console):
  - Cluster ARN — from `lightbulb-{env}-container` outputs
  - Two private-subnet IDs — from `lightbulb-{env}-network` outputs
  - Paperclip task SG ID — `lightbulb-{env}-paperclip` output
    `PaperclipTaskSecurityGroupId`

## Run Command

Replace `<placeholders>` with the values above. `--task-definition` uses
just the family — ECS resolves to the latest revision automatically.

```bash
aws ecs run-task \
  --cluster <cluster-arn> \
  --launch-type FARGATE \
  --task-definition lightbulb-<env>-paperclip-migrate \
  --network-configuration "awsvpcConfiguration={subnets=[<priv-subnet-1>,<priv-subnet-2>],securityGroups=[<paperclip-task-sg>],assignPublicIp=DISABLED}" \
  --profile isol8-admin --region us-east-1
```

The command prints the started task ARN. Capture it (e.g. `TASK_ARN=...`)
for the status + log commands below.

## Wait for Completion

The migrate task should reach `STOPPED` with exit code `0`:

```bash
aws ecs describe-tasks \
  --cluster <cluster-arn> \
  --tasks "$TASK_ARN" \
  --query 'tasks[0].{status:lastStatus,exitCode:containers[0].exitCode,reason:stoppedReason}' \
  --profile isol8-admin --region us-east-1
```

Repeat until `status` is `STOPPED`. A non-zero `exitCode` (or a non-empty
`reason` other than the normal `Essential container in task exited`)
means the run failed — see "Failure Recovery" below.

## Tail Logs

While the task is running, or after it stops, stream CloudWatch logs:

```bash
aws logs tail /lightbulb/<env>/paperclip-migrate --follow \
  --profile isol8-admin --region us-east-1
```

The first lines should show `apt-get` installing `postgresql-client`,
followed by `CREATE EXTENSION` output, followed by the Drizzle migrate
runner.

## When to Run

- **Once on first deploy**, before treating the Paperclip service as
  healthy. The main service starts up immediately on `cdk deploy`, but
  it will hit empty/missing tables until migrations have run.
- **After every `cdk deploy`** of `lightbulb-{env}-paperclip` that bumps the
  Paperclip image tag — a new release may include schema changes.

## Safety net (when migrate is forgotten)

If you skip the migrate step after a `cdk deploy` that bumped the Paperclip
image, the new task definition lands on the service but the new image hits
schema `N+1` against a database still at `N`. Paperclip's startup queries
fail, `/api/health` returns 5xx, ECS detects the unhealthy task, the
deployment circuit breaker (`circuitBreaker: { rollback: true }`) reverts
the service to the previous task definition, and the old image keeps
serving traffic.

**How to spot this** when reviewing a deploy:

```bash
aws ecs describe-services \
  --cluster <cluster-arn> \
  --services lightbulb-<env>-paperclip-server \
  --query 'services[0].deployments[*].{status:status,rollout:rolloutState,running:runningCount,failed:failureReason}' \
  --profile isol8-admin --region us-east-1
```

A `rolloutState=FAILED` or `failureReason` mentioning circuit breaker is
the signal.

**Recovery:** run the migrate task (the Run Command section above), then
force a new deployment so ECS re-applies the latest task definition:

```bash
aws ecs update-service \
  --cluster <cluster-arn> \
  --service lightbulb-<env>-paperclip-server \
  --force-new-deployment \
  --profile isol8-admin --region us-east-1
```

## Failure Recovery

### Common failure modes

**1. Stale Drizzle lock from a crashed prior run**

If the migrate task was force-stopped or crashed mid-transaction, Drizzle
may leave a stale row in `__drizzle_migrations` with `finished_at IS NULL`.
Postgres rolled back the SQL changes (each file is in a transaction), but
the lock-tracking row may still be present.

Diagnose:

```sql
SELECT id, hash, created_at, finished_at
FROM __drizzle_migrations
WHERE finished_at IS NULL
ORDER BY created_at DESC
LIMIT 5;
```

If a row exists with `finished_at IS NULL`, recover with:

```sql
DELETE FROM __drizzle_migrations WHERE finished_at IS NULL;
```

Then re-run the migrate task — Drizzle will pick up from the last
fully-committed migration.

**2. Mid-run cancellation**

Drizzle wraps each migration file in a single Postgres transaction. A
cancelled run leaves the schema at the last fully-committed file boundary;
no partial DDL is applied. Re-running the migrate task picks up at the
next unapplied file.

If the cancellation happened mid-multi-file run, you may also see Mode 1
(stale `finished_at IS NULL` row) — apply that recovery first.

**3. Lock contention from concurrent runs**

Drizzle takes a Postgres advisory lock at the start of each migrate run.
If two operators kick off `run-task` near-simultaneously, the second run
blocks until the first finishes (or its connection drops). This is safe
— Drizzle won't apply the same migration twice — but the second run's
log will show "waiting for advisory lock" until the first completes.

Avoid by checking for an in-flight migrate task before kicking off a new
one. Note: there is no ECS service for the migrate task, so list standalone
tasks via `--family`:

```bash
aws ecs list-tasks \
  --cluster <cluster-arn> \
  --family lightbulb-<env>-paperclip-migrate \
  --desired-status RUNNING \
  --profile isol8-admin --region us-east-1
```

If `taskArns` is non-empty, wait or use `aws ecs describe-tasks` on the
returned ARN to see how far along it is.

### General checklist after a failed run

1. Did the task stop cleanly? Check `lastStatus == STOPPED` and
   `containers[0].exitCode == 0`.
2. If `exitCode != 0` or `stoppedReason` is anything other than
   `Essential container in task exited`, pull the CloudWatch logs
   (`aws logs tail /lightbulb/<env>/paperclip-migrate`) and read the last
   ~50 lines.
3. Common log patterns:
   - `connection terminated` / `password authentication failed` — secret
     rotation issue; check the `paperclip_db_credentials` secret.
   - `extension "vector" is not available` — wrong Aurora engine version;
     bug in the cluster definition, not a transient failure.
   - `relation already exists` after a partial-apply — see Mode 1.
4. Drizzle migrations are idempotent for already-applied steps. Once
   you've cleared any stale lock row, re-running the same
   `aws ecs run-task` is always safe.

## Why Manual?

A CDK custom resource (Lambda) could automate this on every deploy, but
adds significant complexity (Lambda image, IAM, longer deploy cycle,
rollback semantics on failed migrations). The team has chosen the
manual run-task path for the initial cutover; the
`migrateTaskDefinition` is exposed publicly on `PaperclipStack` so a
future custom resource can wire to it without re-defining the task.
