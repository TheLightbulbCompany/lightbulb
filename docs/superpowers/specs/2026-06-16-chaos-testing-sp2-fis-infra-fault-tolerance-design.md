# Chaos Testing — Sub-project 2: Infra Fault Tolerance (AWS FIS)

**Date:** 2026-06-16
**Status:** Approved design, pending implementation plan
**Goal:** Verify isol8 *recovers* from real infrastructure and network faults — container death, CPU saturation, DynamoDB throttling, backend outage, Paperclip/Aurora failure, and billing-provider unreachability — by injecting each fault against **dev** and **asserting** recovery automatically.
**Siblings:** SP1 (logic invariants) + SP3 (API contract fuzzing) are merged. SP4 (load) is the remaining track.

## Why this is the track that matters for "resilient"

SP1/SP3 prove the code is correct *in isolation* (hermetic). They say nothing about what happens when AWS misbehaves. Every major isol8 prod incident on record — MCP connect-storm CPU crash-loop + healthcheck SIGKILL, EFS SQLite corruption, version drift, Paperclip WS-401/heartbeat saga — is an **infra/runtime** failure SP1/SP3 structurally cannot catch. SP2 targets exactly that class.

## Hard constraints

- **Dev only. Never prod. Never PR-CI.** On-demand game-day execution.
- Every experiment carries a **CloudWatch stop-condition** (auto-abort) and a **least-privilege FIS IAM role**.
- The harness **refuses to run** unless it confirms the dev account/environment.
- Shared-infra scenarios run only in a **dev quiet window** (no active dev testing).

## §1 — Architecture

Three pieces:

1. **FIS templates in CDK** — new `apps/infra/lib/stacks/fis-stack.ts` with one `aws_fis.CfnExperimentTemplate` per scenario, each referencing existing observability-stack page-alarms as stop-conditions, plus a dev-scoped FIS execution role. Templates are free until run.
2. **Orchestration harness** — `apps/backend/tests/chaos/fis/`, pytest, marked `fis` (excluded from the default suite + CI). Per scenario: (a) establish steady state — drive a real chat through the dev WS gateway and confirm it works against a dedicated **test user/container**; (b) start the FIS experiment via boto3; (c) poll the observable recovery signal; (d) **assert** the recovery SLO; (e) confirm steady state is restored.
3. **Safety guard** — a shared fixture asserting `ENVIRONMENT == "dev"` (and the dev AWS account id) before any experiment starts; refuses otherwise. FIS stop-conditions are the backstop.

## §2 — Scenario catalog (8)

Each scenario = FIS action + steady-state definition + recovery SLO + blast radius.

| # | Scenario | FIS action | Recovery SLO (assert) | Blast radius |
|---|----------|-----------|------------------------|--------------|
| 1 | **Per-user container kill mid-chat** | `aws:ecs:stop-task` | Gateway pool re-discovers IP (`discover_ip`) + reconnects; in-flight chat resumes or cleanly errors→recovers; container row consistent; < ~30s | **Single container** (safest) |
| 2 | **CPU-stress crash-loop** | `aws:ecs:task-cpu-stress` | Under sustained saturation the healthcheck does NOT SIGKILL-loop the container (validates loosened healthcheck + MCP fixes); chat degrades then recovers when stress lifts | Single container |
| 3 | **DynamoDB throttle/unavailable** | `aws:fis:inject-api-throttle-error` (or route disruption to dynamodb) | Backend retries/degrades with clean errors; no 5xx storm; recovers when throttle lifts | **Shared dev** |
| 4 | **Backend kill / gateway drop** | `aws:ecs:stop-task` (backend, desiredCount=1) | ECS restarts; ALB drains; clients reconnect; gateway recovers within SLO | **Full dev backend** |
| 5 | **Paperclip Fargate kill** | `aws:ecs:stop-task` (Paperclip service) | Teams BFF + heartbeat relay degrade gracefully and recover after restart | **Shared dev (Teams)** |
| 6 | **Aurora failover/unavailable** | `aws:rds:failover-db-cluster` | Provisioning + Teams survive a DB failover blip; reconnect to the new writer | **Shared dev (Paperclip)** |
| 7 | **backend↔Paperclip network partition** | `aws:network:disrupt-connectivity` | The relay recovers after the partition heals (exercises the WS-401/heartbeat path directly) | **Shared dev (Teams)** |
| 8 | **Billing provider unreachable** | `aws:network:disrupt-connectivity` (egress to Stripe/Metronome endpoints) | Provision gate + billing path **degrade gracefully** (clean blocked-state, no wedge/crash); recover when egress restored | **Shared dev (billing)** |

## §3 — Prerequisites & CDK changes

- **Scenario 2 (CPU-stress) needs `enableExecuteCommand` (SSM) on the per-user ECS tasks** — task-level FIS actions inject via the SSM agent. Small `container-stack.ts` / `ecs_manager.py` change to enable it on the test path. Confirm the per-user task role permits SSM messages.
- **Stop-condition alarms:** reuse existing page-alarms where they exist; add narrowly-scoped alarms only where a scenario needs a signal that doesn't exist yet (each ~$0.10/mo, trivial).
- **FIS IAM role:** least-privilege — only the actions/resources each template needs, dev-scoped.

## §4 — What is explicitly NOT in SP2

- **Deep Stripe/Metronome failure-mode testing** (throttling, partial errors, webhook replay/idempotency, subscription lifecycle under provider errors) — that is **application-layer**, belongs to the deferred **SP1 billing-handler follow-up** (hermetic, respx-stubbed). FIS cannot fault a SaaS API; scenario 8 only tests the *network-unreachable* case. SP2 must not touch real (even test-mode) billing state.
- **Load / concurrency at scale** — SP4.
- **Prod anything.**

## §5 — Cost

No recurring cost (templates/roles/alarms are free or trivial). Per-run only, dev, on-demand: FIS action-minutes (~$0.10/action-minute → cents per scenario) + minimal dev Bedrock inference for the driven chats (cents–low dollars). Stop-conditions cap worst-case Bedrock spend. Does not change the monthly bill.

## §6 — Implementation sequencing

One spec, sequenced plan by blast radius (build + validate isolated scenarios before shared-infra ones):

1. **Framework + Scenario 1** (container kill — single-container, cleanest). Establishes the harness + safety guard + FIS-stack scaffold.
2. **Scenario 2** (CPU-stress) — after the `enableExecuteCommand` prereq lands.
3. **Scenarios 5–7** (Paperclip Fargate/Aurora/partition).
4. **Scenarios 3, 4, 8** (shared-infra: DDB throttle, backend kill, billing egress) — last, with the dev-quiet-window discipline.

## Open risks

- **moto/local can't model these** — SP2 is inherently against live dev; there is no hermetic shortcut. The harness must be robust to dev being mid-deploy (skip-with-reason, not false-fail).
- **task-level FIS actions on Fargate** require SSM and have documented limitations; the plan verifies each action works on Fargate before relying on it.
- **Shared-infra scenarios disrupt all dev users** for their window — the harness must announce + gate on the quiet window, and stop-conditions must be conservative.
- **Aurora failover** is destructive to in-flight Paperclip connections by design; assert recovery, not zero-disruption.
