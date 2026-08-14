# Page-tier alarm runbook

What to do when a page fires at 3am. One entry per page-severity alarm.

Pages go to the `isol8-{env}-alerts-page` SNS topic → `admin@isol8.co`.
Warn-tier alarms go to `isol8-{env}-alerts-warn` → same mailbox.

**If you got paged but see nothing wrong, check that alerting is alive
before assuming it was a blip.** These topics ran with zero subscribers for
weeks once already:

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-east-1:877352799272:isol8-prod-alerts-page
```

A subscription is only real when `SubscriptionArn` is **not** the literal
string `PendingConfirmation`. SNS deletes unconfirmed email subscriptions
after 3 days and CloudFormation does not notice.

## Orientation

```bash
export AWS_PROFILE=isol8-admin        # aws sso login --profile isol8-admin

# What is currently broken
aws cloudwatch describe-alarms --state-value ALARM \
  --alarm-name-prefix isol8-prod- --region us-east-1 \
  --query "MetricAlarms[].[AlarmName,StateUpdatedTimestamp]" --output text

# Backend logs (single log group for the FastAPI service)
aws logs tail /ecs/isol8-prod --since 30m --region us-east-1
```

Dashboard: CloudWatch → Dashboards → `isol8-prod-orr`. Chat success rate and
p99 are the two SLO widgets at the top; if both are healthy, customers are
probably fine and the page is infrastructure-only.

Cluster is `isol8-prod-container-ClusterEB0386A7-Uc0EwgAC9DcQ`. Per-user
containers are ECS services named `openclaw-{user_id}-{hash}`.

---

## P1 — container-error-state

A per-user OpenClaw container is stuck or erroring.

Blast radius is one customer, not the fleet, so this is rarely a
wake-up-and-fix. Find the owner from the backend logs, then check their
service. Most stuck containers clear with a reprovision — see the
`prod-org-reprovision` skill, which preserves billing, Clerk org and
marketplace rows. Do **not** hand-edit the container; the platform owns it.

If several fire at once it is not a per-user problem — look at P12, W39 and
the EFS alarms instead.

## P2 — stripe-webhook-sig-fail

Stripe webhook signature verification failed.

Two causes, very different urgency. Either the signing secret drifted (a
rotated key in the Stripe dashboard that never made it to Secrets Manager)
or someone is posting forged webhooks. Compare the endpoint's signing secret
in the Stripe dashboard against `isol8/prod/stripe_webhook_secret`. If they
match, the traffic is hostile and the correct response is to look at source
IPs, not to touch the secret.

Signature failures mean the events were **rejected**, so nothing was
mis-billed. The risk is missed events, not bad ones.

## P3 — workspace-path-traversal

A path-traversal attempt against the per-user workspace was blocked.

The attempt failed — this is a security signal, not an outage. It fires on
event, so a single datapoint is a single attempt. Identify the actor in the
backend logs and decide whether to suspend the account. Escalate if the same
owner appears repeatedly.

## P4 — update-fleet-patch-invoked

Somebody invoked a fleet-wide config patch (`PATCH /container/config`).

This is an audit trail, not a failure. If it was you, dismiss it. If it was
not you, treat it as a compromised admin credential: this endpoint rewrites
`openclaw.json` across every container.

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
aws ecs describe-services --cluster isol8-prod-container-ClusterEB0386A7-Uc0EwgAC9DcQ \
  --services <backend-service> --region us-east-1 \
  --query "services[0].{running:runningCount,desired:desiredCount,deployments:deployments[].status}"
```

## P10 — apigw-ws-5xx-rate

WebSocket API 5xx rate above 5%. Chat is broken even if the REST API is fine.

The WS path is longer than the REST path — API Gateway → Lambda authorizer →
VPC Link → NLB → ALB → FastAPI → per-user container. P9 healthy plus P10
firing points at the authorizer or the VPC Link, not the backend. Check the
Lambda authorizer's error and throttle alarms (W33/W34).

## P11 — marketplace-path-traversal

Path traversal blocked during a marketplace agent export. Same posture as P3:
blocked, security signal, identify the seller account.

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
  --state-machine-arn arn:aws:states:us-east-1:877352799272:stateMachine:isol8-prod-provision \
  --status-filter FAILED --max-items 5
```

Read the failed execution's history to find the failing state. Common causes
are EFS access-point creation limits and Fargate Spot capacity droughts —
`ecs_manager` falls back to on-demand on a zero-placement timeout, so a Spot
drought should self-heal; a hard failure usually will not.

---

## Alarms that are noisy on purpose

`W39-fargate-task-stopped` used to fire on every deploy (it compared exit
codes before stop reasons, and a rolling deploy SIGTERMs the container to
exit 143). That is fixed — it now discriminates on ECS `stopCode`. If it
starts firing per-deploy again, that regression is back.

The monthly budget is deliberately set to a tight $1,500 launch tripwire,
below the historical peak. It is expected to trip as spend grows; raise the
limit rather than ignoring it.
