# AWS Agentic AI Lens checklist (Lightbulb)

Source: AWS Well-Architected Agentic AI Lens (10 Jun 2026) and the Prescriptive Guidance agentic series, as summarised in `pivot-research-2026-11-07/11-aws-golden-path.md` and `09-agentcore-golden-path.md`. Each line: `[pillar] rule — where in this repo — how to check`. Every "where" was confirmed with `git grep` on origin/main on 8 Nov 2026. Review a PR against the lines it touches.

## Security
- [SEC] Tenant isolation comes from claims, namespaces and key prefixes, not one runtime per tenant (pool, not silo) — `apps/backend/core/services/agentcore_runtime.py:59` still names a per-owner runtime `lb_<env>_...` — flag any new per-owner always-on resource.
- [SEC] Authenticate every request with a signed JWT verified against the IdP's JWKS — `apps/backend/core/auth.py:24` (`_get_cached_jwks`) — new routes must depend on it, not read an owner id from the body.
- [SEC] Delegate with the user context carried as claims, never by assuming the user's role — `apps/backend/routers/runtime_internal.py:333` (`effect_check` takes `owner_id`) — new effects pass through `effect_check`.
- [SEC] Per-operation limits live on the transaction, not the service role — `apps/infra/lib/stacks/container-stack.ts:157` (secret ARN scoped by `${aws:PrincipalTag/owner_id}`) — new per-owner grants use a principal tag, not a wildcard.
- [SEC] Scope DynamoDB access with `dynamodb:LeadingKeys` — `apps/infra/lib/stacks/api-stack.ts:725`, `:837` — new tables grant a key prefix, not `table/*`.
- [SEC] Rules an agent must obey are enforced in code, not skill prose — `apps/backend/routers/company_policy.py:5` (mode read by `effect_check`) — grep the skill text for "never" rules with no server check.
- [SEC] Secrets come from Secrets Manager into the task, never the image or repo — `apps/infra/lib/stacks/service-stack.ts:2571` (`secrets:`) — `git grep` the diff for key-shaped strings.
- [SEC] Scan every push for committed secrets — `.github/workflows/secret-scan.yml:2` (gitleaks) — workflow must stay in the kept set.
- [SEC] CI reaches AWS by OIDC role, no long-lived keys — `apps/infra/lib/app.ts:94` (`lightbulb-github-actions`) — no `AWS_ACCESS_KEY_ID` in workflows.
- [SEC] Put a WAF in front of the public edge — `apps/infra/lib/stacks/network-stack.ts:140` (`AlbWebAcl`) — new public ALBs get the association at `:238`.
- [SEC] Encrypt tenant data with a customer key — `apps/infra/lib/stacks/api-stack.ts:571` (`messagingKey`) — new PII tables set `encryptionKey`.
- [SEC] Throttle at the edge per environment — `apps/infra/lib/stacks/api-stack.ts:311` (`THROTTLE_CONFIG`) — new stages inherit it.
- [SEC] Agent tasks run in private subnets with no public IP — `apps/infra/lib/stacks/service-stack.ts:2614` (`assignPublicIp: false`) — new services copy it.
- [SEC] Voice caller identity is verified, owner mode needs a PIN or OTP, never caller ID alone — not built yet (grep for `X-Twilio` returns nothing) — the voice PR must add the signature check and a test.

## Reliability
- [REL] Deploys roll back automatically — `apps/infra/lib/stacks/service-stack.ts:2611`, `:2654`, `:2691` (`circuitBreaker: { rollback: true }`) — new ECS services set it.
- [REL] Durable agent work runs as Temporal workflows with bounded retries — `apps/backend/core/temporal/automations.py:194` (`RetryPolicy(maximum_attempts=3)`) — every new activity sets a `RetryPolicy`.
- [REL] Retried side effects carry an idempotency key — `apps/backend/core/billing/metronome_client.py:275` — new money or send effects take one.
- [REL] Queues have a dead-letter queue and a max receive count — `apps/infra/lib/stacks/api-stack.ts:655`, `:964` — new SQS consumers copy it.
- [REL] Production data survives stack deletion and has point-in-time recovery — `apps/infra/lib/stacks/database-stack.ts:26`, `:112` — new prod tables are not `ephemeral`.
- [REL] Cap Lambda concurrency so one tenant cannot starve the rest — `apps/infra/lib/stacks/service-stack.ts:2235`, `apps/infra/lib/stacks/openrouter-gate-stack.ts:70` — new Lambdas set `reservedConcurrentExecutions`.

## Operations
- [OPS] Alarm on the failure modes of every new workflow — `apps/infra/lib/stacks/observability-stack.ts:531` (`createAlarm`) — each new service adds an `AlarmDef`.
- [OPS] Logs are structured JSON — `apps/backend/core/observability/logging.py:61` (`JsonFormatter`) — new code logs through it.
- [OPS] Set a retention on every log group — `apps/infra/lib/stacks/service-stack.ts:1566` (`TWO_WEEKS`), `api-stack.ts:432` — new groups set `retention`.
- [OPS] Tag every resource for cost allocation — `apps/infra/lib/lightbulb-stage.ts:399` (`Project`, `Environment`) — new stacks live inside `LightbulbStage`.
- [OPS] Alert on account spend — `apps/infra/lib/stacks/observability-stack.ts:2775` (`MonthlyBudget`) — check the threshold still fits the bill.
- [OPS] Trace across agent boundaries with W3C context — task role has X-Ray write at `apps/infra/lib/stacks/container-stack.ts:220`, but no OpenTelemetry in `apps/backend/pyproject.toml` — add tracing before the voice path ships.

## Performance and cost
- [COST] Dev runs on Spot — `apps/infra/lib/stacks/container-stack.ts:18` (`DEV_SPOT`), used at `service-stack.ts:2604` — prod services stay on-demand, dev ones use it.
- [COST] Graviton (ARM) is 20% cheaper than x86 — no `cpuArchitecture` anywhere in `apps/infra/lib` — set ARM64 on new Fargate tasks and build arm64 images.
- [COST] Avoid per-NAT cost; run the fewest NAT gateways — `apps/infra/lib/stacks/network-stack.ts:40` (`natGateways: 1`) — do not raise it.
- [COST] Use on-demand billing for spiky tables — `apps/infra/lib/stacks/database-stack.ts:100` (`PAY_PER_REQUEST`) — new tables copy it.
- [COST] Pick the model by task: cheap model for routing and formatting, premium for reasoning — `apps/backend/core/services/hermes_profiles.py:80` (`cheap_model_ref`) — new LLM calls choose a tier on purpose.
- [COST] Memory has a lifecycle, no unbounded growth — `apps/backend/core/services/memory_api.py:278` (wipe rows with `expiresAt`) — new per-contact memory sets a retention rule.
- [COST] Nothing idles per tenant — `apps/infra/lib/stacks/container-stack.ts:94` (`containerInsights: false`) and per-owner runtimes at `agentcore_runtime.py:59` — a new resource that bills while idle needs a stated reason.

## Sustainability
- [SUS] Scale to zero or share fixed overhead across tenants — `apps/infra/lib/stacks/service-stack.ts:2705` autoscaling min 1 dev, 2 prod (`autoScaleTaskCount`) — new always-on tasks justify their floor in the PR.
