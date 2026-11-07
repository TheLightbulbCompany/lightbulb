# Phase 2 — Golden-path infra (lane-e plan, 8 Nov 2026)

Source: master plan Phase 2 row and invariant 5; spec §4.1 (Workers row), §4.6 (pool tenancy); research 09, 11, 13, 14 rows 5, 8, 9, 10, 14.
Gate: 24 h of `AWS/Bedrock-AgentCore MemoryUsed-GBHours` ≈ 0 outside live use; Secrets Manager < 20; Cost Explorer floor ≤ $250/mo.

What the code does today (origin/main e1ee420e51):
- Worker turns already get their own AgentCore session per task when the super-agent switch is on and no rotating credential is still the VM's to refresh (`runtime_runs.session_kind_for`, `token_custody.task_vm_allowed`); the task session is stopped after the seal (`learning.close_task_attempt` → `stop_task_session`). Router turns use the workspace session (`session_id_for(owner)`).
- What still keeps a per-owner workspace session warm: the 15 min idle timeout (`agentcore_runtime.IDLE_TIMEOUT_S`), the typing prewake and new-room prewake (`routers/messaging.py`), and the learning workflow's `curate_specialist`, which wakes the workspace VM after every task that merged learning (cold start ~55 s + the idle tail), although the curator only comes due every 7 days.

## Task 1 — Pooled sessions: nothing keeps a per-owner session warm (PR 1, highest value)

Files: `apps/backend/core/services/agentcore_runtime.py`, `core/services/runtime_transport.py`, `routers/messaging.py`, `core/temporal/learning.py`, their tests.

- `IDLE_TIMEOUT_S` 900 → 300 s; `WARM_MARGIN_S` 300 → 100 s (a third of the window, as today). Reaches a live runtime on its next roll (lifecycle drift, `hermes_provisioning.py:1175`).
- Delete the typing prewake and the new-room prewake. Voice/huddle/meeting prewakes stay: they start a real call or meeting.
- `curate_specialist` runs only when the workspace session is already warm (the backend's `warm_until` estimate); cold → `{"ran": false, "skipped": "cold"}`, no invoke. The bridge's own idle pass (`turn._curate_when_idle`) still curates whenever the workspace VM runs a turn.
- Not changed: the custody gate (a second VM would spend a single-use refresh token); Worker turns run in task sessions wherever it allows.
- Tests: the constants; typing no longer calls `prewake_owner`; `curate_specialist` does not call `runtime_runs.curate` when cold, does when warm.

## Task 2 — Memory retention (PR 2)

Files: `core/services/workspace_memory.py`, a sweep caller (the existing Temporal schedule or the backend's daily loop), tests.

- Expire long-term records older than N = 90 days since last update (`updatedAt`, else `createdAt`) per owner namespace, batched through `BatchDeleteMemoryRecords`.
- Caveat: "not recalled in N days" needs a last-recalled time. AgentCore records carry none and runtimes search Memory directly (the backend never sees their recalls), so this PR uses last-updated. A true recall-based rule needs the runtime plugin to stamp recall hits (follow-up).
- Tests: the cutoff picks only stale records; the deleter is called in batches of 100; a namespace error does not stop other owners.

## Follow-up tasks (not in this lane's PRs)

3. **One shared runtime per tier (v2, `platformVersion: V2`)**: ARN from settings, not `container["agentcore_runtime_arn"]`; tenant id carried in every request; delete `provision_hermes_runtime` on org.created, per-owner IAM roles, `roll_hermes_runtimes` per owner, the wake lease once v2 cold start is measured (unpublished; measure first).
4. **AgentCore Identity** for per-business OAuth (Google built-in; Square/Calendly/Zenoti custom OAuth2), `userId` = Clerk org; retire `oauth_dance.py` refresh and per-owner boot secrets.
5. **Secrets → ~10**: website logins and API keys to DynamoDB + KMS envelope (`website_logins.py`, `integration_secret_store.py`); delete per-owner boot ×28, OpenRouter ×48, Apps ×60.
6. **Network**: one ARM Fargate task (2 for HA) in prod, Spot in dev; public subnets + SGs instead of the NATs; CloudFront free plan instead of WAF; profile the 40–55 % idle CPU.
7. **Curator without the workspace VM**: run the curator inside the next task session's write window or in the merge activity, so a phone-only business still curates.
8. Medical-ready items (BAAs, zero retention, calendar-only Google client) stay with the founder/lead.

## Metric

`AWS/Bedrock-AgentCore MemoryUsed-GBHours` (CloudWatch, us-east-1, per runtime and summed): after Task 1 rolls, a dormant workspace should show 0 between live uses and ≤ 5 min of tail after each.
