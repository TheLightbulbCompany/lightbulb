# Page-tier alarm runbook

What to do when a page fires at 3am. One entry per page-severity alarm.

Pages go to the `lightbulb-{env}-alerts-page` SNS topic → `admin@isol8.co`.
Warn-tier alarms go to `lightbulb-{env}-alerts-warn` → same mailbox.

**If you got paged but see nothing wrong, check that alerting is alive
before assuming it was a blip.** These topics ran with zero subscribers for
weeks once already:

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-1:877352799272:lightbulb-prod-alerts-page
```

A subscription is only real when `SubscriptionArn` is **not** the literal
string `PendingConfirmation`. SNS deletes unconfirmed email subscriptions
after 3 days and CloudFormation does not notice.

## Orientation

```bash
export AWS_PROFILE=isol8-admin        # aws sso login --profile isol8-admin

# What is currently broken
aws cloudwatch describe-alarms --state-value ALARM \
  --alarm-name-prefix lightbulb-prod- --region us-east-1 \
  --query "MetricAlarms[].[AlarmName,StateUpdatedTimestamp]" --output text

# Backend logs (single log group for the FastAPI service)
aws logs tail /ecs/lightbulb-prod --since 30m --region us-east-1
```

Dashboard: CloudWatch → Dashboards → `lightbulb-prod-orr`. Chat success rate and
p99 are the two SLO widgets at the top; if both are healthy, customers are
probably fine and the page is infrastructure-only.

The ECS cluster carries the **platform** only — the backend service, the SFN
provisioner task and Paperclip. There are no per-user ECS services: each
workspace is an AWS Bedrock **AgentCore runtime** named
`lb_prod_<sanitised owner>_<12 hex>`, and you inspect one with
`aws bedrock-agentcore-control get-agent-runtime`, not `describe-services`.
Discover the cluster arn rather than pasting one — the suffix changes on stack
recreation:

```bash
aws ecs list-clusters --query 'clusterArns[?contains(@,`lightbulb-prod-container`)]' --output text
```

---

> **P1 (container-error-state) and P3 (workspace-path-traversal) no longer
> exist.** Both emitters lived in the deleted `core/containers/` package and
> both alarms were removed with it. The surviving path-traversal trip-wire is
> **P11**, below.

## P2 — stripe-webhook-sig-fail

Stripe webhook signature verification failed.

Two causes, very different urgency. Either the signing secret drifted (a
rotated key in the Stripe dashboard that never made it to Secrets Manager)
or someone is posting forged webhooks. Compare the endpoint's signing secret
in the Stripe dashboard against `lightbulb/prod/stripe_webhook_secret`. If they
match, the traffic is hostile and the correct response is to look at source
IPs, not to touch the secret.

Signature failures mean the events were **rejected**, so nothing was
mis-billed. The risk is missed events, not bad ones.

## P4 — update-fleet-patch-invoked

⚠️ **This alarm is silent and cannot fire.** Its only emitter was the
fleet-wide `PATCH /container/config` endpoint, which was deleted with the
`openclaw.json` config rail — a microVM has no config file to rewrite. **PR-6b
deletes the alarm** (in flight); until that lands, do not read its silence as
coverage. There is no fleet-wide config door to audit any more: an agent's
config is its S3 Hermes profile, written per owner.

## P5 — debug-endpoint-prod-hit

A `/debug/*` endpoint was reached in production. These are supposed to 403.

A hit means either the `ENVIRONMENT` setting is wrong on the running task
(so the guard thinks it is dev) or someone is probing. Check the task's
environment first — a misconfigured `ENVIRONMENT` also affects metric
dimensions and provisioning behaviour, so it is worth ruling out fast.

## P6 — billing-pricing-missing-model

A chat used a model with no pricing row, so the usage cannot be rated.

This is a silent revenue leak: inference happened and we cannot bill it. Add
the missing pricing entry. Until then, every request on that model is free.
Not customer-impacting, so it can wait for morning, but it should not wait
for next week.

## P7 — update-worker-stalled

The update worker's heartbeat has been absent for 5 minutes.

The scheduled worker loop is started by the backend lifespan handler, so a
dead loop usually means a wedged backend rather than a worker-specific bug.
Check P12 first — if the backend liveness heartbeat is also gone, this is a
symptom, not the cause. Restarting the backend service restarts the loop.

## P8 — dynamodb-throttle-sustained

DynamoDB throttling for 2 consecutive minutes.

All tables are on-demand, so sustained throttling means a hot partition or a
genuine traffic spike rather than an under-provisioned table. Find the table
from the per-table W40/W41/W42 warn alarms, which carry the table name. A
retry storm in the backend can also cause this — check whether request
volume actually rose or whether the same call is looping.

## P9 — alb-5xx-rate

Backend 5xx rate above 5%.

Real customer impact. Check the backend logs and whether a deploy just
landed — the deploy pipeline auto-rolls back on circuit breaker, so if a bad
rollout caused it, ECS may already be recovering. Confirm running task count
and health:

```bash
aws ecs describe-services --cluster "$CLUSTER" \
  --services <backend-service> --region us-east-1 \
  --query "services[0].{running:runningCount,desired:desiredCount,deployments:deployments[].status}"
```

## P10 — apigw-ws-5xx-rate

WebSocket API 5xx rate above 5%. Chat is broken even if the REST API is fine.

The WS path is longer than the REST path — API Gateway → Lambda authorizer →
VPC Link V2 → ALB → FastAPI (there is no NLB). Only the desktop local-session
relay is left on this door. P9 healthy plus P10
firing points at the authorizer or the VPC Link, not the backend. Check the
Lambda authorizer's error and throttle alarms (W33/W34).

## P11 — marketplace-path-traversal

Path traversal blocked during a marketplace agent export — a bad agent id OR
a bad seller id, both halves of the profile key (`agent_export._refuse_bad_id`).
Blocked, so it is a security signal rather than an outage: identify the seller
account and escalate if the same one appears repeatedly. **This is now the
platform's only path-traversal page** — its EFS-side sibling (P3) went with
`core/containers/workspace.py`.

## P12 — backend-liveness

**The most serious page here.** The backend has emitted no
`gateway.running.count` sample in 10 minutes. That metric is emitted every
~60s regardless of workload, so its absence means the backend's metrics loop
is dead — usually the whole service.

This alarm treats missing data as breaching, which is correct: silence *is*
the signal. Check the service is running and look at recent task stops.
Everything else in this file is downstream of the backend being up, so start
here if multiple alarms fire together.

## P13 — app-unreachable

The product front door (`flickmeon.ai` / `dev.flickmeon.ai`) is failing its
Route53 health check from multiple AWS regions. **Users cannot load the
product.**

Route53 probes from several regions every 30s and flips only after 3
consecutive failures, so this is never a single blip or one bad region. The
front door is Vercel, so check Vercel status and the last frontend deploy
before touching AWS — and remember prod frontend only ships on a
`workflow_dispatch`, so a recent merge is not a suspect.

## P14 — api-unreachable

The backend API (`api.flickmeon.ai` / `api-dev…`) is failing its Route53 health
check on `/health` from multiple AWS regions. **Chat, billing and provisioning
are all down.**

`/health` is the only public path that answers 200 without a Clerk token, so a
failure here is the whole REST door, not one route. Same 3-strike multi-region
rule as P13. Pair it with P9 and P12: P14 + P12 quiet is the backend; P14 alone
with P12 healthy points at the API Gateway / VPC Link / ALB chain in front of it.

## P15 — provision-inline-timeout

An inline provision attempt hit its 1800s hard timeout.

By the time this fires **one owner has already been without a workspace for the
whole 30 minutes**, and recovery now rides the SFN leg — or nothing at all, in
the "unwatched attempt" degrade where `StartExecution` failed and the inline
child was the only thing running. Any occurrence pages. The 2026-08-20 founder
outage had exactly this signature and paged nobody: the metric landed on a
dimension stream no alarm watched (W1 reads only `status=ok/error`) and the SFN
watchdog SUCCEEDED after taking the row over, so the executions-failed pages
never fired either. Start from the owner id in the backend logs, then read the
Step Functions execution history for that claim.

## P17 — alert-subscriptions-page-topic

The page topic has fewer than its expected number of **confirmed**
subscriptions. **Nobody is receiving pages.**

Prod's page tier expects 2 destinations (the alert mailbox AND PagerDuty);
every other tier/env expects 1. The usual cause is that the confirmation mail
was never clicked and SNS deleted the subscription after three days — which is
exactly the silent failure the orientation block at the top of this file warns
about, now watched hourly by a Lambda instead of by memory. Fix it by
re-subscribing and clicking the link; a subscription is only real when
`SubscriptionArn` is not the literal string `PendingConfirmation`.

## P19 — messaging-worker-errors

The `openclaw-work-consumer` Lambda (SQS event source on `openclaw-work.fifo`,
`scripts/work_lambda.py`) recorded `Errors` in 3 of the last 5 minutes. **Room
work is not draining, so agents are not being woken for room messages.**

Per-message failures are reported as batch items, not errors, so `Errors` means
the function itself cannot run: an init/import crash (read its log group for the
traceback — often the SSM/Secrets environment rebuild) or a 180 s timeout. A
consumer that is never invoked (mapping disabled or deleted) does not raise this;
it shows up as api-stack's `openclaw-work-oldest-age` page.

## P20 — messaging-queue-wait-p99

p99 wait **before** the worker picks work up exceeds 30s — it is falling behind
the queue.

Distinct from `openclaw-work-oldest-age` (api-stack), which measures a queued
pointer across retries and visibility waits. This metric measures time before
each delivery begins.

## P21 — messaging-reply-latency-p95

p95 time from a human message to the agent message answering it exceeds 5
minutes (the stated SLO).

This ends at backend reply handling, before browser delivery and paint, and
only includes messages that receive a matched reply. The older 54–57s cold
invoke measurement is historical, not a current baseline. This alarm does not
distinguish cold starts from model/tool work or catch every slow interaction;
use the [chat performance checks](chat-performance.md) for that investigation.

## P22 — messaging-ingress-4xx

The Hermes bridge ingress rejected a dispatch with a 4xx.

**Non-transient and silent:** a 4xx is OUR bug — bad agent id, wrong auth,
malformed body — it will not self-heal, and every affected message never reaches
the agent. Any occurrence pages. The classic cause is a contract skew between a
promoted backend and an un-rolled runtime image, so check what
`get-agent-runtime` reports for `containerUri` against `hermes-version.json`
before looking anywhere else.

## P30 — paperclip-run-setup-failed

A Paperclip-woken run (routine, issue, Teams Run) failed with `setup_failed`
before the agent ever ran. Emitted by `routers/paperclip_events.py` as
`paperclip.run.failed{error_code=setup_failed}` for every forwarded
`agent.run.failed`, before the push cooldown, so it counts every failure.

This is a platform fault, not a model fault, and it hits every run for the
affected companies. On 2026-09-23 it was "Secret decryption failed" after a
Paperclip master-key change: 15 h of failed runs with no alarm. Read the setup
error in `/lightbulb/prod/paperclip`. If it is decryption, see P32.

Related: W82 (`process_lost`) and W83 (all codes, 10 or more in 15 min) are the
warn-tier alarms on the same metric.

## P31 — paperclip-run-gateway-auth-failed

A Paperclip run failed with `hermes_gateway_auth_failed`: the agent's
`hermes_gateway` adapter `apiKey` no longer matches the owner's hook token.
The adapter config is a snapshot, so rotating a hook token without re-syncing
the adapter (`sync_agent_adapter_tokens` / `repoint_agent_adapter` in
`core/services/paperclip_agent_lifecycle.py`) breaks every run for that
owner. Re-sync the owner's agents. Room chat keeps working, so users may not
report it.

## P32 — paperclip-secret-decrypt-failed

Paperclip logged `Secret decryption failed`. This comes from a CloudWatch Logs
metric filter on `/lightbulb/{env}/paperclip` (namespace
`LightbulbAI/Paperclip/{env}`), so it fires even if the plugin event path is
down. Paperclip's master key no longer decrypts stored company secrets, and
every run that needs one fails at setup (see P30). Check the Paperclip task's
master-key secret and what changed it. The log line carries the master key
fingerprint.

## P33 — paperclip-escalation-exhausted

A Paperclip issue was blocked by recovery after a platform-failed run
(`setup_failed`, `process_lost` or `timeout`), and the backend could not hand it
on. `core/services/paperclip_escalation.py` checks each such issue about 90 s
after the failure and moves it failed agent → escalation agent (the owner's
`PAPERCLIP_ESCALATION_AGENT_OVERRIDES` entry, else the starter Engineering
agent) → root CEO, with one PATCH `{assigneeAgentId, status: "todo"}`. It emits
`paperclip.escalation.exhausted` when the root CEO was the agent that failed, or
when no rung is invokable (paused, terminated, pending approval).

The issue is still `blocked`. Search the backend logs for
`paperclip escalation exhausted` to get the owner, issue and failed agent. The
platform fault is usually the real problem: check P30, P31, P32 and W82 first.
Once runs work again, move the issue to a live agent and set it to `todo`.

Related: `paperclip.escalation.escalated{rung}` counts successful handoffs, and
`paperclip.escalation.refused` counts 422s (the issue had real open blockers,
so it was left alone).

## provision-executions-failed / provision-executions-timedout

A provisioning Step Functions execution failed or timed out. **New signups
are not getting containers.**

Highest business impact on this list during launch — it is invisible to
existing customers and fatal to new ones.

```bash
aws stepfunctions list-executions --region us-east-1 \
  --state-machine-arn arn:aws:states:us-east-1:877352799272:stateMachine:lightbulb-prod-provision \
  --status-filter FAILED --max-items 5
```

Read the failed execution's history to find the failing state. On AgentCore
the common causes are:

- **`CreateAgentRuntime` role validation** — `ensure_agentcore_trust` updates
  the owner role's trust policy seconds before the create and IAM propagates
  slowly, so AgentCore rejects a freshly-trusted role. The runner retries this
  ONE `ValidationException` up to 12 times at 5 s; exhausting that is a real
  failure.
- **`CREATE_FAILED`/`UPDATE_FAILED`** — terminal. AWS will never accept an
  `UpdateAgentRuntime` against it, so the runner deletes the dead runtime and
  clears the row's ARN rather than leaving a retry stuck reusing it forever.
- **A missing `AGENTCORE_HERMES_IMAGE`** — provisioning refuses rather than
  guessing an image. Check the pin flip actually deployed.

---

## Alarms that are noisy on purpose

`W39-fargate-task-stopped` used to fire on every deploy (it compared exit
codes before stop reasons, and a rolling deploy SIGTERMs the container to
exit 143). That is fixed — it now discriminates on ECS `stopCode`. If it
starts firing per-deploy again, that regression is back. It watches the
PLATFORM cluster (backend, provisioner, Paperclip); a per-workspace agent
crash does not reach it, because a microVM is not an ECS task.

The monthly budget is deliberately set to a tight $1,500 launch tripwire,
below the historical peak. It is expected to trip as spend grows; raise the
limit rather than ignoring it.
