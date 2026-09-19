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
`openclaw.json` config rail — a microVM has no config file to rewrite. The
alarm itself is still declared in `observability-stack.ts` and should be
removed; until it is, do not read its silence as coverage. There is no
fleet-wide config door to audit any more: an agent's config is its S3 Hermes
profile, written per owner.

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
