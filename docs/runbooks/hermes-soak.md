# Hermes-on-AgentCore soak runbook

Run for **24 hours after a workspace flips** to `runtime_host=agentcore`
(`scripts/migrate_workspace_to_hermes.py`, M6 Task 1/3 of
`docs/superpowers/plans/2026-09-16-hermes-agentcore-migration-M6-migrate-rollout-delete.md`).
One pass per migrated workspace. A red line **blocks the next cohort's flip**
until fixed and re-verified (M6 Task 4 Step 3) — do not run `--all` on more
workspaces with a known-red line.

Every item cites the file/table/metric it's grounded in. Where no signal
exists today, it says so instead of inventing one. Owner id below is the org
owner (`org_...`); the never-migrated baseline is
`org_3JCpE2dm4G8Dip7z5e6Lod5fqVY` — do not expect it on Hermes.

---

## First hour (functional smoke, M6 Task 4 Step 1)

- [ ] **Cold start / bridge health** — Look at: `core/services/container_rpc.py`
  `"health"` RPC, which for an `agentcore` row reaches
  `apps/infra/hermes/bridge/server.py:_health`. Pass:
  `{"ok": true, "payload": {"ready": true, "runtime": "hermes", ...}}` within
  ~54-57s (`runtime_transport.py:185-190`). `readiness_state.py:104-109`
  reports a running AgentCore row `"ready"` unconditionally without probing,
  so use the RPC directly, not `/container/status`, to prove liveness.
  Rollback trigger: `NOT_READY` or a transport error past 90s.
- [ ] **Room turn** — Look at: post in `#general`; poll
  `lightbulb-{env}-messaging` `OWNER#<owner>/RUN#<runId>`. Pass:
  `messaging.run.ended{outcome:"success"}` (`messaging.py:2202`) after
  `messaging.run.callback{outcome:"applied"}` (`messaging.py:2222-2224`);
  reply threaded, run frames visible mid-turn. Rollback trigger:
  `outcome=error`, or the run never starts (see sweeper item).
- [ ] **Node exec** — Look at: trigger `system.run`; watch `relay.connect` /
  `relay.presence` (`routers/gateway_relay.py`) for the paired Mac's socket
  and the transcript for stdout. No success metric exists yet for
  `core/services/node_broker.py` (M2) — **no signal today; add
  `node.invoke.outcome` there as a follow-up.** Pass: output returned within
  the command timeout. Rollback trigger: timeout, or `hello-ok` never
  negotiated (spec design doc, report-02 checklist).
- [ ] **Browser** — Look at: panel shows the AgentCore Browser live view,
  agent navigates. M5 has not landed on this branch — ground against
  `...-M5-browser-shopify.md` and spec §5 once merged; **manual visual check
  only, no metric yet.** Rollback trigger: panel never connects, or navigate
  silently no-ops.
- [ ] **Paperclip run** — Look at: trigger a routine/issue run from Teams.
  Pass: `run.completed{outcome:"success", source:"paperclip"}`
  (`run_analytics.py:132`); confirm it doesn't feed alarm **W67**
  `agent-run-failure-storm` (`observability-stack.ts`: `run.completed
  {outcome:failure, source:paperclip}` > 20/15min). Rollback trigger:
  `outcome=failure`, or W67 trips within the hour.
- [ ] **Roster wiring intact** — Look at: `paperclip-companies` row, keyed
  by **user_id PK** (via `by-org-id` GSI for an `org_` owner —
  `paperclip_repo.py:1,50,70`). Pass: `starter_agent_ids` unchanged, each
  role has a matching S3 profile under `<owner>/hermes/<agent_id>/` (what
  `migrate_workspace_to_hermes.py:_starter_roles()` reads). Rollback
  trigger: a role missing its profile, or wrong model tier answering. Known
  collision: a human member of **two orgs** can resolve the wrong personal
  row (2026-09-14 picker-bug root cause) — check if this workspace has such
  a member.
- [ ] **Slack** — Look at: round-trip a message on a bound channel. Only
  failure counters exist (`slack.forward_failed`, `slack.revoke_failed` —
  `routers/slack_app.py`), no success metric. Pass: message lands, neither
  fires. Rollback trigger: `slack.forward_failed` on this workspace.
- [ ] **Agent mail** — Look at: send to `{agent}@{slug}.<mail domain>`
  (`AGENT_EMAIL_DOMAIN`), confirm it lands via `GET
  /api/v1/messaging/rooms/{roomId}/messages`. **No signal today** —
  `agent_email.*` metrics cover outbound send/autoreply, not inbound
  landing; add `agent_email.inbound.landed` in the SES-inbound handler as a
  follow-up. Rollback trigger: mail never lands within a few minutes.
- [ ] **Marketplace** — Look at: deploy a listing
  (`catalog_service.deploy_from_artifact`), new agent responds. Pass:
  `catalog_deploy.agent_entry_keys_dropped` (`catalog_service.py:536`) does
  NOT fire, agent answers. Rollback trigger: metric fires, or no working
  profile.
- [ ] **Usage row written** — Look at: `lightbulb-{env}-usage-counters`,
  key `{owner_id, period}`. Pass: `messaging.usage.callback{outcome:"applied"}`
  (`messaging.py:2257-2281`) for the turn above, counters row increments.
  Rollback trigger: counters row not incrementing at all (`outcome=room` is
  expected for a room turn — that path writes usage via `_after_run_ended`).

---

## First day

- [ ] **Idle park + wake** — Look at: `lightbulb-{env}-containers` row,
  `last_interactive_at`. Pass: `wake_policy.is_parked_trial`
  (`wake_policy.py:112-120`) only parks after `agentcore_runtime.
  IDLE_TIMEOUT_S` = **4h** idle, and only for `trial_local` — a paid
  workspace never parks by this predicate. Next interaction should reproduce
  the cold-start item. Rollback trigger: a non-trial workspace reads parked,
  or a trial workspace never wakes on the next poll/send.
- [ ] **Heartbeat / sweeper closes = 0 unexpected** — Look at:
  `lightbulb-{env}-messaging` GSI `by-run-deadline` (`sweep="open"`,
  `deadlineAt<now`, `messaging_repo.py:1741-1751`). Pass: `messaging.run.swept`
  (`run_ledger.py:37/40/66`) stays at zero for this owner all day — every run
  got `run.started` within 120s and a heartbeat every 30s inside the 90s
  deadline (Global Constraints). Rollback trigger: any
  `{error:"agent_did_not_start"}` or `{error:"agent_stopped_responding"}` for
  this owner — the bridge accepted ingress but never called back.
- [ ] **Gate spend vs cap** — Look at: log group
  `/lightbulb/{env}/bedrock-gate`, namespace `LightbulbAI/BedrockGate` (dim
  `env`): `gate.allowed` vs `gate.trial_daily_cap` / `gate.trial_expired` /
  `gate.credit_exhausted` / `gate.ledger_unprovisioned`
  (`lambda/bedrock-gate/{metrics,errors,core}.mjs`). Also check
  `core/billing/bedrock_gate_tokens.py` Ruling M4-R16: a warm AgentCore
  session caches its boot-secret bearer, so any rotation on this owner MUST
  have called `StopRuntimeSession` before the old token was denied — verify
  via `_agentcore_presentable_hashes` (`:297`) rather than assume it took.
  Pass: no unexpected deny spike; a rotation (if any) left invoke working.
  Rollback trigger: live workspace denied with no matching rotation, or
  `GateErrorAlarm` / `GateHoldReleaseFailedAlarm` fires (`bedrock-gate-stack.ts:397-419`).
- [ ] **Runtime session CPU/memory** — **No signal today.** The execution
  role has `cloudwatch:PutMetricData` scoped to namespace `bedrock-agentcore`
  (`container-stack.ts:426`, confirms the namespace) but this repo defines
  no metric names/dashboard for it — check AWS Console → CloudWatch →
  Metrics → `bedrock-agentcore` for this owner's `runtimeSessionId`
  (`runtime_transport.session_id_for`). Follow-up: add a widget in
  `observability-stack.ts`. Also confirm by hand (no metric for this either)
  that only **one** session id is ever live — two sessions for one workspace
  silently corrupt the litestream replica set (`runtime_transport.py:18-23`).
- [ ] **No `request_dump_*` leaked to S3** — Look at:
  `aws s3 ls s3://lightbulb-{env}-openclaw-replicas/<owner>/hermes/ --recursive | grep request_dump`.
  Pass: zero matches — `bridge/sync.py` `EXCLUDE_GLOBS` (`sessions/request_dump_*.json`,
  `logs/*`, `.lightbulb-turn/*`, `media/messaging/*`) must keep Hermes's
  unconditional API-error dumps off S3. Rollback trigger: any match — treat
  as a secrets-hygiene incident, not just a bug.
- [ ] **`.revision` re-pull after a config patch** — Apply
  `PATCH /container/config/{owner_id}`, confirm the running profile picks it
  up. Spec'd in M4 (`...-M4-config-providers-provisioning.md:766-969`: writer
  bumps `<owner>/hermes/<agent_id>/.revision` LAST, bridge's
  `sync.ensure_profile` polls it) but not yet in `apps/infra/hermes/bridge/`
  on this branch — confirm the file behaves this way before relying on this
  line. Pass: next turn reflects the patch. Rollback trigger: patch never
  takes within one turn cycle.

---

## Monitor for one hour after each cohort's flip (M6 Task 4 Step 4)

Existing dashboards/alarms only — nothing new for this milestone.

- [ ] **Run ledger error rate** — `messaging.run.ended{outcome:"error"}` and
  `messaging.run.swept` (both above), for the just-flipped cohort. No
  dedicated alarm exists — **use CloudWatch Metrics Explorer on namespace
  `LightbulbAI` for now; consider a widget mirroring W67 as a follow-up.**
  Expected: no step-change correlated with the flip timestamp.
- [ ] **Bedrock-gate admit/deny rate** — `LightbulbAI/BedrockGate`
  `gate.allowed` vs the deny family (above). No single panel exists; the two
  page alarms (`GateErrorAlarm`, `GateHoldReleaseFailedAlarm`,
  `bedrock-gate-stack.ts:397-419`) are the only wired signal. Expected: no
  step-change correlated with the flip; if either alarm fires within the
  hour, treat it as this flip's fault until proven otherwise (CLAUDE.md:
  debug post-deploy from current logs/rows, not a before/after diff).

---

## Rollback decision

**Who:** the person who ran the flip, with the workspace owner's (or, for a
prod cohort, on-call's) go-ahead — never automated.

**When (any one of):** a red line above that a re-verify doesn't clear;
`messaging.run.swept` fires more than once in an hour for this owner; a
page-tier alarm (`GateErrorAlarm`, `GateHoldReleaseFailedAlarm`, W67)
correlates with this workspace's traffic; a `request_dump_*` leak or any
secrets-hygiene finding; the owner reports the workspace materially worse
than before.

**Command** (lands in M6 Task 1/3 of this plan — `migrate_workspace_to_hermes.py`
does not exist yet on this branch; confirm it exists before relying on this):

```bash
cd apps/backend
uv run python scripts/migrate_workspace_to_hermes.py --owner-id <id> --rollback
```

Restores `containers.runtime_host = "ecs"` and calls
`ecs_manager.start_user_service(owner_id, force=True)` — does **not** delete
the uploaded S3 profiles or the AgentCore runtime, so a re-flip is cheap.
Does not touch billing, Clerk, or the Paperclip company row.

**Before flipping the NEXT cohort after a rollback:** fix the root cause,
re-run this checklist clean on the rolled-back workspace, then resume
`--all` (M6 Task 3) or the next single `--owner-id`. Never proceed with a
known-red line (M6 Task 4 Step 3).

**Prod-dispatch precondition** (once this is a prod cohort — verbatim from
CLAUDE.md "`queue: max`"):

```bash
gh run list --repo TheLightbulbCompany/lightbulb --workflow deploy.yml --limit 10 \
  --json status --jq '[.[]|select(.status!="completed")]|length'
# dispatch only when this reads 0
```
