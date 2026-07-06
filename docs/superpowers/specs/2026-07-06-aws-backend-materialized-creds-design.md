# AWS integration: backend-materialized credentials — Design

**Date:** 2026-07-06
**Status:** Draft (pending user review)
**Scope owner:** platform / infra
**Supersedes:** the credential-delivery mechanism (`credential_source=EcsContainer`)
of `2026-07-05-aws-integration-scope-tiers-design.md`. The scope tiers, ViewOnly
ARN fix, trust-pin gate concept, and connect UX from that spec are shipped and
stay. Only how credentials reach the agent changes.

## Summary

Live validation of the aws_sts rail (spec §4 of 2026-07-05, executed 2026-07-06)
failed at the first `aws` call from the agent with:

> `No credentials found in credential_source referenced in profile isol8-readonly`

Root cause (adversarially verified, see Evidence): OpenClaw's host-env sanitizer
strips `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` from every agent exec child, so
`credential_source = EcsContainer` can never resolve its base credentials from an
agent exec. This is upstream, deliberate, and good: it is the wall that keeps the
agent away from the ECS task role. The shipped design required the agent to reach
the task role in order to bootstrap the AssumeRole — it fought the wall and lost.
It was never an IAM problem: the error is raised locally by botocore before any
network call, and the live IAM chain was verified intact.

**The redesign inverts the flow: the backend performs the `sts:AssumeRole`
(server-side, with the ExternalId it already stores) and materializes the
resulting temporary credentials into the same config file the agent already
reads, refreshed on a schedule.** The agent sees exactly one credential set — the
connector role's — and structurally cannot reach anything else.

## The credential model (the point of the design)

Two identities, hard-walled by OpenClaw's sanitizer — which becomes the
enforcement boundary rather than the obstacle:

| | Identity | Delivery | Consumers |
|---|---|---|---|
| Node process | ECS task role | metadata endpoint (`AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`) | Bedrock inference, EFS, logs, ECS Exec — infra only |
| Agent exec | connector role (`isol8-connector`) | static temp creds in the materialized config file | the agent's `aws` CLI — the only AWS identity the agent has |

The sanitizer strips every env credential channel from the exec child
(`AWS_CONTAINER_CREDENTIALS_*`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`) while allowlisting `AWS_CONFIG_FILE`. **A file is the only
credential channel that survives** — so file-materialized connector creds are not
one option among several; they are the only mechanism that can work, and the
task role is unreachable from the agent by construction.

## Evidence (verified 2026-07-06; all four claims survived adversarial review)

- **Sanitizer, at the deployed tag** (`v2026.6.10-isol8.2`, = prod + dev image
  base): `AWS_CONTAINER_CREDENTIALS_{RELATIVE,FULL}_URI` are in
  `blockedOverrideOnlyKeys` and absent from `allowedInheritedOverrideOnlyKeys`
  (`host-env-security-policy.json:249-250,268-272`), so they land in
  `blockedInheritedKeys` and are dropped from the exec child
  (`bash-tools.exec.ts:1572-1584`). Plugin hooks cannot re-inject them —
  `filterPluginExecEnv` silently drops blocked keys (`bash-tools.exec.ts:124-143`).
  No config/env escape hatch exists at that tag; the fork diff touches zero
  sanitizer files.
- **The error is pre-IAM**: botocore raises `"No credentials found in
  credential_source referenced in profile …"` locally when the container
  provider finds no URI env var (`botocore/credentials.py:1857-1870,2068-2088`)
  — before any `AssumeRole` network call. An IAM failure would read
  `AccessDenied when calling the AssumeRole operation`.
- **The IAM chain was intact**: live, the founder's per-owner task role holds
  `owner-aws-sts` allowing `sts:AssumeRole` on exactly
  `arn:aws:iam::877352799272:role/isol8-connector`, and that role's trust pins
  exactly that per-owner role + ExternalId. Had base creds resolved, the assume
  would have succeeded.
- **Config-file static creds work** (empirically tested): with `env -i` and a
  config file containing only `[profile isol8-readonly]` +
  `aws_access_key_id/aws_secret_access_key/aws_session_token`, the CLI
  succeeded; `aws configure list` shows `TYPE=config-file`. Officially
  supported (AWS CLI userguide, cli-configure-files). The CLI re-reads the file
  on every invocation (empirically: rewrite between two invocations picked up
  the new key). Static creds are never cached in `~/.aws/cli/cache`.
- **The plugin keeps working unchanged**: `resolve.js` matches only the
  line-anchored `[profile isol8-readonly]` header — never the body — and
  re-reads the file on every call. A static-creds body still triggers
  `AWS_PROFILE=isol8-readonly` injection.
- **Backend can already call AssumeRole**: the backend task role has
  `sts:AssumeRole` on `*` (sid `StsAccess`, `service-stack.ts:742-745`, present
  since the original ServiceStack), no permissions boundary, no Deny anywhere,
  and SCPs cannot apply (we are the org management account). The **only**
  blocker is the connector role's trust policy, which currently pins the
  per-owner container role.
- **Sessions are capped at 1 hour, hard**: backend task-role creds are
  themselves role-session creds, so backend `AssumeRole` is role chaining (ECS
  is absent from AWS's exemption list, which names only user credentials and
  EC2 instance profiles); chaining caps sessions at 3600s regardless of
  `MaxSessionDuration`. Every deployed connector stack also pins
  `MaxSessionDuration: 3600`. `DurationSeconds=3600` succeeds; more fails with
  `ValidationError`.
- **Expiry is a clean failure** (empirically tested with genuinely expired
  creds): `An error occurred (ExpiredToken) … The security token included in
  the request is expired`, exit non-zero. (Exact wording varies per service —
  never string-match it.)
- **Blast radius of the trust migration is one stack**: exactly one aws_sts
  connection exists fleet-wide (the founder's self-account connector,
  PowerUser tier, stack created 2026-07-06). No customer stacks to migrate.

## Design

### 1. Acquisition — backend does the AssumeRole

A scheduled backend pass (and the `aws-complete` handler, immediately on
connect) performs, per connected owner:

1. Read `{role_arn, external_id, region}` from Secrets Manager
   (`isol8/{env}/integrations/{owner}/aws` — where it already lives; the
   backend already has `GetSecretValue` via `IntegrationSecretsRW`).
2. `sts:AssumeRole(role_arn, ExternalId=external_id, DurationSeconds=3600,
   RoleSessionName=isol8-{owner-hash})` using the stable source role (§3).
3. Hand the resulting `{AccessKeyId, SecretAccessKey, SessionToken}` to the
   materializer (§2).

Owners are enumerated from the secret-free `integrations` DDB status table
(`provider=aws`, `status=connected`). **Gap found during due diligence:**
`integrations_repo` today has only per-owner accessors (`get`,
`list_for_owner`); the table is `owner_id` PK / `provider` SK with no
provider GSI, so cross-owner enumeration needs a new
`list_by_provider(provider)` — a paginated `Scan` with a filter. Fine at
current scale (7 rows fleet-wide); a provider GSI is the upgrade path if the
table ever grows enough for Scan to matter.

### 2. Delivery — same file, new body

`materializers/aws_cli.py` writes the **same path** (`owner_efs_root/.aws/config`
= `/home/node/.openclaw/.aws/config` in-container, already pointed to by the
deployed `AWS_CONFIG_FILE` task env) with a **fully replaced** profile body:

```ini
[profile isol8-readonly]
region = {region}
aws_access_key_id = {AccessKeyId}
aws_secret_access_key = {SecretAccessKey}
aws_session_token = {SessionToken}
```

- **No `role_arn`/`credential_source`/`external_id` lines.** A profile mixing
  `role_arn` with static keys is treated by botocore as an assume-role profile
  — the body must be replaced wholesale, not appended.
- **The ExternalId no longer touches EFS at all** (today it sits in the config
  file). Strict security improvement: exfiltrating the file now yields ≤1h
  session creds instead of a durable role_arn+ExternalId pair.
- The profile stays **named** — never `[default]` — so in-process Bedrock stays
  on the task-role default chain, untouched. Same 0600 + chown-1000 +
  `os.replace` atomic write as today.
- Profile name stays `isol8-readonly` even at PowerUser tier: the plugin,
  resolver, and materializer pin it, and renaming means an image rebuild for
  zero functional gain. (Same call as the role-name misnomer in the 07-05 spec.)

### 3. Trust — a stable source principal, not the raw backend role

The backend task role's ARN carries a CDK-generated suffix
(`…-TaskRole30FC0FBB-Bke6duRtFNPZ`) — pinning it in connector trust policies
couples every connected account to a CDK logical ID that changes if the role is
ever replaced. Instead, CDK (service-stack) mints one tiny stable-named role per
env:

- **`isol8-{env}-connector-source`** — trust: the backend task role;
  permissions: `sts:AssumeRole` on `arn:aws:iam::*:role/isol8-connector` only.
  No other grants.

The backend assumes `connector-source`, then uses those creds for the connector
`AssumeRole` (both hops are role chaining; each session capped 1h — fine, we
request 3600). Connector trust policies — ours and every future customer's —
pin `isol8-{env}-connector-source`: a deterministic, human-auditable ARN that
survives backend-role churn.

Sites that change in lockstep (all reference the same helper):
- `aws_cfn.py`: `owner_task_role_arn(owner_id)` → `connector_source_role_arn()`
  (env-derived, no owner parameter — one principal for all owners; per-owner
  isolation lives in the ExternalId condition, which stays).
- `connections.py:282` — quick-create link + `aws-setup` response inject the new
  principal.
- `connections.py:114` — the self-account trust-pin gate expects the new
  principal (it currently 400s any trust doc not pinning the per-owner role).
- `aws-readonly-connector.yaml` — parameter *description* text only (the
  principal is a pure `!Ref`); README drift fix (it still claims two tiers).

**Rejected alternatives:** (a) pin the raw backend role ARN — unstable, above;
(b) trust our account root + ExternalId (the common SaaS pattern) — loses the
exact-principal pin that the trust-pin gate is built on; (c) web-identity
federation (`AWS_WEB_IDENTITY_TOKEN_FILE` *is* sanitizer-allowlisted, and the
CLI would self-refresh) — requires hosting an OIDC issuer plus an IAM OIDC
provider resource in every connected account; disproportionate. Revisit only if
sub-hour refresh proves operationally painful.

### 4. Refresh — push, not pull

The agent cannot request a refresh (the sanitizer wall cuts both ways), so the
backend keeps the file perpetually fresh:

- **Cadence: every 15 minutes**, as a pass in the existing
  `update_service.run_scheduled_worker` loop (60s tick), gated by the same
  `time.monotonic()` pattern as `_paperclip_purge_pass`. This is the sanctioned
  scheduled path — no new long-running worker. Guaranteed credential runway at
  any instant: **≥ 45 minutes**.
- Refresh also fires inline on `aws-complete` (connect ⇒ instantly usable file).
- Idempotent + double-run tolerant (two backend tasks during a rolling deploy
  both writing is benign — `os.replace`, same content class).
- A refresh failure for one owner logs + emits a metric and does not kill the
  pass; the previous creds remain valid until expiry.

**Operational model (verified against `main.py`, `update_service.py`,
`service-stack.ts`, backend `Dockerfile`):**

- **Exactly one refresh loop per backend task.** uvicorn runs single-process
  (no `--workers` flag in the Dockerfile CMD), and the worker is one asyncio
  task created once in the FastAPI lifespan (`main.py:367`). No intra-task
  duplication.
- **Exactly one backend task steady-state.** The backend service is
  `desiredCount: 1` with no autoscaling (`service-stack.ts:1375` — required
  independently by the in-process WS fanout design). During a rolling deploy
  (`minHealthyPercent: 100, maxHealthyPercent: 200`) two tasks briefly overlap
  and both may run a pass: benign — same SM secret in, same-class creds out,
  `os.replace` atomic writes, STS calls idempotent. No leader election needed
  at N=2-for-minutes; if the service ever scales out for real, passes overlap
  harmlessly (last writer wins with equally-valid creds) — correctness never
  depends on single-writer.
- **Task death self-heals inside the runway.** ECS replaces the task
  (desiredCount=1); the lifespan restarts the worker; the refresh pass uses
  the purge-pass `time.monotonic()` sentinel pattern, which **always runs on
  the first iteration after process start** — so creds are re-freshened
  within ~60s of the replacement task booting. Replacement takes minutes;
  the runway is ≥45. Same math covers deploys and crash-loops shorter than
  ~45 min.
- **The worker cannot die silently.** Each pass is individually
  try/except-wrapped AND the whole iteration is wrapped
  (`update_service.py:run_scheduled_worker`), with an
  `update.scheduled_worker.heartbeat` metric every iteration; the task handle
  is registered in `system_health.BACKGROUND_TASKS`, whose `.done()` /
  `.exception()` state the health surface reports. The refresh pass adds its
  own success/failure metrics (per-owner refresh failure alarm-able in the
  observability stack).

**Failure modes, quantified:**
- Backend down > ~45 min → agent's next `aws` call fails with the clean
  `ExpiredToken` error; self-heals on the next successful pass. This is a new
  dependency the old design did not have (CLI-side refresh needed no backend)
  — accepted: the same backend outage already takes down chat, updates, and
  billing, so agent AWS access degrading with it adds no new availability
  class.
- A single long-running CLI invocation (e.g. a big `aws s3 sync`) started near
  session end dies mid-run with `ExpiredToken` — creds are resolved once per
  process. Documented, accepted; runway floor is 45 min.
- Reprovision preserves EFS, so the file survives container replacement and the
  next pass keeps it live — the rehydration gap flagged as Open Question 2 in
  the 07-05 spec closes for free.
- EFS close-to-open consistency bounds rewrite visibility to seconds —
  immaterial at this cadence.

### 5. What gets retired (security posture strictly improves)

The per-owner container role loses its AWS-rail job entirely:

- `bedrock_aip.put_owner_aws_sts_policy` / `delete_owner_aws_sts_policy` (the
  per-owner `owner-aws-sts` inline grant + its self-account-deny-drop logic) —
  retired for this rail. Disconnect kill-switch becomes: stop refreshing +
  delete the config file + delete the SM secret + status row. Outstanding
  sessions expire ≤ 1h — the same revocation bound the old design had.
- The #1155 self-account carve-outs revert: the boundary
  `BoundaryDenyAssumeIntoOurAccount` returns to **unconditional** (drop the
  `aws:PrincipalArn` exemption in `container-stack.ts`), and the inline-deny
  drop logic goes. Defense-in-depth is restored to full breadth: no per-owner
  container role can assume into our account, ever, founder included.
- The trust-pin gate **stays** (repointed at `connector-source`): it is still
  what makes self-account connection safe — only a role whose trust pins our
  assume principal + the owner's ExternalId can be connected.

### 6. Migration

- **Our account (the only live connection):** one CloudFormation parameter
  update on the existing `isol8-connector` stack — `TrustedPrincipalArn` →
  `isol8-prod-connector-source` ARN, `UsePreviousValue` for ExternalId,
  re-acknowledge `CAPABILITY_NAMED_IAM`. No template redeploy, no reconnect, no
  container change.
- **Future connections:** get the new principal automatically via the
  quick-create link. No existing customer stacks exist to migrate.
- **Ordering:** backend + CDK ship first (the refresh pass fails AccessDenied,
  logged, harmless) → stack parameter update → next pass succeeds and rewrites
  the file → agent works. The stale `credential_source` file body is replaced
  by the first successful pass; no manual EFS surgery.

## What does NOT change

- The connect-flow CFN stack in the target account — still required; it *is*
  the connector role. One-time per account, quick-create link, same UX.
- The extended image, the `aws-exec-profile` plugin, the fork — zero changes.
- `container-stack.ts` env (`AWS_CONFIG_FILE` already deployed) — no change for
  delivery (only the §5 boundary revert touches this stack).
- Scope tiers, picker, ViewOnly ARN fix, ExternalId minting — all stay.
- Bedrock / node-process credential chain — untouched.

## Acceptance criteria

From a gateway agent exec (chat path, where the plugin fires):

1. `aws sts get-caller-identity` returns the **connector role** ARN
   (`…assumed-role/isol8-connector/…`), not the task role.
2. Tier enforcement spot-check unchanged from the 07-05 spec (write denied on
   read tiers, permitted on `power_user`).
3. **Refresh proof:** force a refresh pass, then `aws sts get-caller-identity`
   from the agent again — new session (different access-key id), no container
   action taken.
4. **Expiry proof (or simulation):** with refresh disabled and creds expired,
   the agent's call fails with `ExpiredToken` and recovers after one pass.
5. Node process unaffected: no `AWS_PROFILE` in process env; Bedrock inference
   still works.

## Rollout

1. **CDK:** `connector-source` role (service-stack) + boundary-deny revert
   (container-stack). One deploy.yml ride.
2. **Backend:** materializer body swap + refresh pass + `aws_cfn.py` helper +
   `connections.py` principal/gate updates + retire `owner-aws-sts` machinery.
   Same deploy (backend image ships as a CDK asset in deploy.yml).
3. **Ops:** the one-parameter CFN stack update on `isol8-connector` (manual,
   ~2 min, admin console or CLI).
4. **Live validation:** re-run the acceptance list — this time the runtime path
   has been proven at every link before the end-to-end attempt.
