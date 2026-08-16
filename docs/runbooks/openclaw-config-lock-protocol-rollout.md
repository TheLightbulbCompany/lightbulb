# OpenClaw Config-Lock Protocol Rollout

This rollout removes legacy persistent `openclaw.json.lock` sidecars without
breaking mutual exclusion with a live backend writer. It is intentionally two
phase. Do not combine or reverse the phases.

## Phase 1: prepare backend writers

1. Merge the backend-only protocol-preparation PR. The automatic main pipeline
   deploys dev first and prod second.
2. Wait for the complete pipeline. In each environment, verify the backend ECS
   service reached steady state and no task from the prior backend revision is
   still running.

Phase 1 leaves the legacy persistent `fcntl` sidecar in place. Every backend
writer takes the separate `.isol8-guard`; after recovery publishes either the
`migrating-v1` or `ephemeral-v1` marker, those same writers switch to
OpenClaw's ephemeral `O_EXCL` sidecar protocol. It is independently safe to
leave deployed indefinitely.

## Phase 2: deploy recovery, then refresh owner clones

1. Merge the stacked recovery PR only after phase 1 is steady in both
   environments. The automatic pipeline installs the recovery base and admin
   operation in dev, then prod. This does not touch existing owner clones.
2. Wait for the complete phase-2 pipeline. In both environments, verify the
   backend ECS service and `ContainerStack` are steady before queueing any
   owner rollout.
3. From an authenticated platform-admin session, submit to dev:

   ```http
   POST /api/v1/container/updates
   Content-Type: application/json

   {
     "owner_id": "all",
     "update_type": "task_definition_refresh",
     "description": "OpenClaw config-lock protocol rollout",
     "changes": {}
   }
   ```

   Use `https://api-dev.isol8.co` for dev. The endpoint returns `queued` and
   `skipped` counts.
4. Verify the complete dev owner rollout. Only then submit the same request to
   `https://api.isol8.co` and verify the prod owner rollout.

Updating the CDK base task definition alone is insufficient: every owner runs
a per-owner clone. The queued operation re-registers each clone from the latest
base while carrying its current CPU, memory, environment, secrets, task role,
and EFS access point. It updates the ECS service with the existing stop-first
single-writer deployment configuration and waits, with a 15-minute bound, for
that owner to become stable before the worker advances. A scaled-to-zero
service stores the new revision and runs recovery on its next normal wake.
Every later boot also takes the guard and removes an ephemeral sidecar abandoned
by a crashed backend or the stopped prior OpenClaw task, so the original
zero-byte-lock outage cannot recur after the transition.

The backend service has desired count one. Queue only after its deployment is
steady. Each update carries a fenced apply lease and persists its target task
definition before changing ECS. A replacement backend reclaims an expired
lease and converges the service on that same ARN without
`forceNewDeployment`, so a crash at any point cannot create a second rollout.
Do not intentionally deploy the backend while this fleet operation is in
progress; recovery is for unplanned turnover, not a reason to add avoidable
churn.

## Verification and recovery

- Confirm the admin response accounts for the expected container rows.
- Monitor pending-update rows until every `task_definition_refresh` row is
  `applied` or `failed`.
- For running owners, verify ECS service stability and the boot log line
  `[boot] config lock protocol transition complete`.
- For stopped owners, verify the container row points at the new task-definition
  revision; confirm the boot log line after the next wake.
- A missing or pre-phase-2 CDK base command fails the owner update closed before
  re-registration. A recovery error exits container startup before OpenClaw.
- Investigate failed rows, then submit the fleet request again. The protocol
  transition and base refresh are safe to repeat; owners with an in-flight
  task-definition roll are reported as skipped and can be picked up by the
  retry request after that roll completes.

Do not delete sidecars directly and do not run a direct AWS mutation script.
