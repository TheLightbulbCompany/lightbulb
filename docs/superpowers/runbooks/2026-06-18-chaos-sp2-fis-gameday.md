# Chaos SP2 / FIS — Dev Game-Day Runbook

**Status:** ready to run. FIS infra + the 8 scenario harnesses are deployed/merged and verified. This runbook is the operator's checklist to actually execute the chaos tests against **dev**.

**What "running" does:** each scenario injects a REAL fault against dev via AWS FIS, then the harness asserts recovery (a real chat turn succeeds again). Shared-infra scenarios disrupt all dev users for their window. **Run only in a dev-quiet window, with a human watching.** Never against prod (the FIS templates exist only in the dev CDK stage; the harness has a two-gate dev-only guard).

---

## What's already done (verified)

- **8 FIS experiment templates live on dev** (`isol8-dev-fis-s1…s8`), each with the curated stop-condition set (P9 alb-5xx + P10 apigw-5xx + W10 chat-error, + P1 container-error for container scenarios — ≤5, NOT_BREACHING, won't self-trip). Successful template creation = FIS validated their structure. The FIS execution role is assumable by `fis.amazonaws.com`.
- **Harnesses merged** at `apps/backend/tests/chaos/fis/` (`test_s1…s8`), marked `fis` + behind the autouse dev-only guard. `mint_test_token()` does the real Clerk ticket→session-JWT exchange.
- **Observability foundation** (Phases 1–3) deployed: the stop-condition alarms are correct and the recovery signals (chat.e2e.latency, gateway/Aurora/Paperclip alarms) are live.

## Scenario status

| Scenario | Runnable now? | Prereq if not |
|---|---|---|
| S1 container-kill | ✅ (start here — single container, safest) | container tagged (below) |
| S3 ddb-throttle | ✅ shared-dev | — |
| S4 backend-kill | ✅ full-dev-backend | — |
| S5 paperclip-kill | ✅ shared-dev (Teams) | — |
| S7 backend↔paperclip partition | ⚠️ wide blast | narrow scope (below) |
| S8 billing-egress | ⚠️ wide blast | narrow scope (below) |
| S2 cpu-stress | ⛔ skips | dedicated chaos task def with SSM-agent sidecar + ECS Exec disabled |
| S6 aurora-failover | ⛔ skips | a serverlessV2 reader on the dev Paperclip Aurora cluster |

---

## Pre-run setup (one-time)

1. **Dedicated dev chaos user + container.** Create a dev Clerk user (NOT `isol8-e2e-testing@mailsac.com` — that's the E2E gate's), take it through `/chat` so a container + Friday/Atlas provision. Record its owner id → `CHAOS_FIS_USER_ID`, and the agent id → `CHAOS_FIS_AGENT_ID` (defaults to `friday`).
2. **Tag the chaos container** so S1/S2 target only it (not a real user's):
   ```bash
   # find the per-user service's task, tag the task (or the service) isol8:owner=CHAOS_TEST_USER
   aws ecs tag-resource --resource-arn <chaos-task-arn> \
     --tags key=isol8:owner,value=CHAOS_TEST_USER --profile isol8-admin --region us-east-1
   ```
   Without this tag, S1/S2 resolve to **zero** tasks and would report a false `completed` with no actual kill.
3. **(S7/S8 only) narrow the network scope** — both currently use `scope=all`/`scope=dynamodb` on the shared private subnets (see the `TODO(gameday)` notes in `fis-stack.ts`). For a real test, either accept the wide blast in a fully-quiet dev window, or narrow to a dedicated subnet (S7) / a Stripe+Metronome customer-managed prefix list (S8) first.
4. **(S2/S6) skipped until their infra exists** — see the prereq column. Leave skipped.

## Pre-run safety check (every run)

- Announce the dev-quiet window.
- Confirm the **stop-condition alarms are currently OK** (not already firing) so a real abort is meaningful:
  ```bash
  aws cloudwatch describe-alarms --profile isol8-admin --region us-east-1 \
    --query 'MetricAlarms[?contains(AlarmName,`P9-alb`)||contains(AlarmName,`P10-apigw`)||contains(AlarmName,`W10-chat`)||contains(AlarmName,`P1-container`)].[AlarmName,StateValue]' --output text
  ```
  (Note: `W5`, `W48`, `W50`, `bedrock-meter-dlq` are pre-existing chronic ALARMs unrelated to FIS — they are NOT stop-conditions, so they won't abort experiments.)
- Have the FIS console + the CloudWatch dashboard open.

## Run (one scenario at a time — start with S1)

```bash
cd apps/backend
ENVIRONMENT=dev \
CHAOS_FIS_CONFIRM=dev \
CLERK_SECRET_KEY=<dev clerk secret> \
CLERK_ISSUER=<dev clerk issuer, e.g. https://up-moth-55.clerk.accounts.dev> \
CHAOS_FIS_USER_ID=<chaos user/owner id> \
CHAOS_FIS_AGENT_ID=friday \
  uv run pytest tests/chaos/fis/test_s1_container_kill.py -o addopts="" -s -v
```

The harness will: (a) drive a real chat turn (steady state) — **if this fails, stop and fix auth before injecting** (the Clerk FAPI exchange is validated live here for the first time); (b) start the FIS experiment; (c) wait for it to terminate `completed` (a `stopped` means a stop-condition fired = real customer impact → investigate); (d) assert recovery (a fresh chat turn succeeds within the SLO).

**Recommended order:** S1 (safest) → S5 → S7 → S3 → S4 → S8 (most disruptive last). One at a time; let dev settle between runs.

## What to watch

- **FIS console:** experiment state `running` → `completed` (not `stopped`/`failed`).
- **The stop-condition alarms** (P9/P10/W10/P1): if any trips, FIS auto-aborts — that's a real customer-impact signal worth investigating, not just a test artifact.
- **The recovery assertion** in the pytest output (the `done` frame returns).
- **chat.e2e.latency / gateway / Aurora / Paperclip alarms** for the recovery shape.

## Post-run

- Confirm steady state restored (a chat turn succeeds; alarms back to OK).
- Remove the chaos tag if you don't want S1/S2 to keep targeting that container.
- Record results (which recovered within SLO, any that `stopped` or missed the SLO → file a resilience bug).

## Honest boundary

This runbook gets us to the point of **proving** the alarms fire + the platform recovers under real faults. Until a scenario is actually run here, "the alarm fires on this fault" and "the platform recovers" remain asserted-by-design, not proven. The first live run also validates the Clerk session-JWT exchange end-to-end.
