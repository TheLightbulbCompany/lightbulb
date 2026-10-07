# Load testing — messaging, agent conversations, session sharing (design, not run)

Owner ruling 2026-10-07: three scenarios, **dev only, nothing runs without Prasiddha's confirmation**.
The repo has no load test today. Delete this spec once the harness ships (CLAUDE.md "Planning docs are disposable").

## 0. Facts every scenario inherits

| Fact | Value | Source |
|---|---|---|
| Dev HTTP API stage throttle | 100 burst / 50 rps, stage-wide, shared by every dev user | `apps/infra/lib/stacks/api-stack.ts` `THROTTLE_CONFIG.dev` |
| Work consumer | batch 1, `maxConcurrency` 20, reserved 20, timeout 180 s | `service-stack.ts` `WorkConsumer` |
| Work FIFO | visibility 240 s, DLQ after 30 receives, group = room+agent | `api-stack.ts` `OpenClawWorkQueue` |
| Projector | DDB stream, batch 10, window 0, bisect on error, **no reserved concurrency** | `api-stack.ts` `MessagingProjector` |
| openrouter-gate | reserved concurrency 25 (streaming: one slot per in-flight model call) | `openrouter-gate-stack.ts` |
| Dev model policy | `deepseek/deepseek-v4-flash`, `max_price` 0.1 prompt / 0.2 completion ($/M) | GH variable `OPENROUTER_MODEL_POLICY_JSON_DEV` |
| Trial key | $0.25/day per owner, OpenRouter-enforced; over it the gate answers 429 `trial_daily_cap` | CLAUDE.md "Managed inference" |
| Cold wake | 54–57 s; single-flight per owner (`WAKE_LEASE_S` 120, losers poll) | `core/services/runtime_transport.py` |
| Messaging table | on-demand; one append = strong read of `META` + conditional transaction, **6 attempts, no backoff** | `messaging_repo.py` `_SEQ_RETRY_ATTEMPTS` |
| Dev baseline (2026-09-20) | queue wait p50 19 ms / p95 23 ms; ingress p50 232 ms / p95 30.5 s; reply p50 13.1 s / p95 57.3 s; projector notify p50 765 ms / p95 1.4 s | `docs/runbooks/chat-performance.md` |
| **One AWS account for dev AND prod** (`877352799272`) | Lambda unreserved concurrency, API GW account rps, AppSync, AgentCore quotas are **shared with prod** | CLAUDE.md "AWS Configuration" |

The last row is the main blast-radius fact: the projector and `ws-message` (unreserved) draw on the
same Lambda pool as prod's projector. Every run stops at 50% of the account concurrency limit.

## 1. Shared harness

| Piece | What | Status |
|---|---|---|
| Load generator | **Round one: k6 on ONE machine** (owner ruling). AWS **Distributed Load Testing** (DLT, CFN, k6 on Fargate) only if one runner cannot make the load. In DLT: **Native mode** only (Standard mode overrides the k6 `options`, dropping arrival-rate executors and thresholds), and every task runs a full copy of the script, so divide the target rate by task count (10 tasks × 40 rps = 400 rps otherwise). | DLT owner-gated |
| Fixtures | `apps/backend/scripts/load/fixtures.py` — Python, imports `Clerk`/`Session`/`dev_only` from `scripts/multiparty_e2e/runner.py` (no second Clerk client). `setup` / `teardown` subcommands. | to write |
| k6 scripts | `apps/backend/scripts/load/k6/{messaging,agents,viewers}.js`, reading `fixtures.json` | to write |
| Mac simulator | `apps/backend/scripts/load/mac_sim.py` (scenario 3) — Python, not k6; see §4 | to write |

**Auth (all scenarios).** Exactly the multiparty runner's sanctioned path, no password, no OTP:

1. `fixtures.py setup` reads `lightbulb/dev/clerk_secret_key`, refuses unless
   `GET /v1/instance` says `environment_type == development` (runner already does).
2. Creates users `load-{nnnn}+clerk_test@example.com` via BAPI `POST /users`
   (`skip_password_requirement`), first name `Ldu{nnnn}` so no message text can match a member
   name (a name opening a message triggers the 2 s Jev addressing call in `core/services/messaging.py` `address()`).
3. Mints a one-time `POST /sign_in_tokens`, redeems it on the dev FAPI
   (`up-moth-55.clerk.accounts.dev`, `_is_native=1`), sets the active org, and writes
   `{userId, sid, clientJwt}` per user to `fixtures.json`.
4. Each k6 VU owns one user and refreshes its own 60 s JWTs: `POST /v1/client/sessions/{sid}/tokens`
   (API) and `…/tokens/lightbulb-appsync` (AppSync) every 40 s, carrying the rotating client JWT
   forward exactly as `Session._post` does.

Pre-checks: `GET /v1/users/count` against Clerk's dev-instance user cap (documented as 100; verify).
Dev org member cap is unlimited (`max_allowed_memberships: 0`). FAPI per-IP limits are unknown and all
DLT tasks share one NAT: the pilot measures them. `fixtures.json` holds live client JWTs: deleted at teardown.

**Fixture orgs.** BAPI `POST /organizations {name, created_by}` fires `organization.created` →
`provision_new_workspace` → the normal SFN provision (Paperclip company, six starter agents,
AgentCore runtime, trial OpenRouter key). Members via `POST /organizations/{id}/memberships`,
sequentially (each fires the Paperclip JOIN + `#general` seat). Load rooms are created by the
harness with `POST /messaging/rooms`; **never post in `#general`** (all six agents `active`).

**k6 shape (all three scripts).** Each message/frame carries the sender's `Date.now()`; receivers
subtract their own (Fargate syncs to Amazon Time Sync, skew ≪ the 100 ms resolution used).
```js
export const options = { scenarios: { s: { executor: 'ramping-arrival-rate', stages: __ENV.STAGES } },
  thresholds: { http_req_failed: ['rate<0.005'], live_delivery_ms: ['p(95)<2000'] } };
// setup(): read fixtures.json; VU i takes users[i] → token refresher (40 s), one AppSync
// socket ported from packages/shared/src/live/appsyncEvents.ts (header-* subprotocol,
// connection_init, subscribe to /ws/<ws>/rooms/<room>/<deliveryKey> and /users/<key>
// from GET /messaging/live-config); default(): the scenario's one action; Trend per hop.
```

## 2. Scenario 1 — messaging load (humans only, no agents)

**Question.** How many messages/s can (a) one room and (b) the whole dev stack accept and
deliver live? Bottlenecks, in order of suspicion:

| Hop | Mechanism | Failure signature |
|---|---|---|
| Send, one room | strong read of `ROOM#…/META` + transaction conditional on `lastSeq`; 6 tries, no backoff | `MessagingRepoError` 5xx; log line `messaging: sequence conflict on room=` |
| Send, all rooms | dev stage 50 rps; backend 2–4 tasks autoscaling at 60% CPU | API GW 429; ALB `TargetResponseTime` |
| Stream → projector | one projector invocation per shard at a time, batch 10 | `IteratorAge`; `messaging.projector.notify.latency` |
| Projector → AppSync | one inbound publish per room event, fan-out to N subscribers | `BroadcastDroppedEvents`, `BroadcastEventServerError` |
| Index queue → AOSS | projector enqueues every message for search; indexer `maxConcurrency` 5 | index DLQ, `MessageSearchBatchStaleAlarm` |

**Shape.** One fixture org, U = 20 → 80 users (bounded by the Clerk cap). Rooms have human
members only, so `select_dispatch_targets` writes no `WORK#` rows, no Jev call, no runtime.
Test users have no Expo tokens, so the projector's push fan-out sends nothing.

| Sub-test | Load | Holds |
|---|---|---|
| 1a hot room | 1 room, concurrent senders 1, 2, 4, 8, 16, each at 1 msg/s | 3 min per step |
| 1b fan-out | 1 sender at 1 msg/s; subscribers 1, 10, 40, 80 | 3 min per step |
| 1c breadth | 20 rooms × 4 members; arrival rate 5 → 40 rps total (under the 50 rps throttle) | 10 min ramp, 10 min hold |
| 1d (optional) | 1c above 50 rps, only after a CDK PR raises `THROTTLE_CONFIG.dev` (§5) | owner-gated |

Each VU also reconciles like a client (`GET …/messages?afterSeq=`) on a detected gap.

**Metrics and pass bar.**

| Metric | Where | Pass |
|---|---|---|
| Send ack (`POST /messaging/rooms/{id}/messages`) | k6 `http_req_duration` | p95 < 500 ms |
| Send → AppSync receipt | k6 Trend `live_delivery_ms` | p95 < 2 s (baseline notify p95 1.4 s) |
| Live delivery completeness | received seqs vs sent; gaps healed by `afterSeq` read | 100% after reconcile |
| Errors | k6 `http_req_failed` (429 counted separately) | < 0.5% in 1a–1c |
| Hot-room ceiling (1a) | first step where 5xx > 0 | **recorded**, not pass/fail |
| Projector | `LightbulbAI messaging.projector.notify.latency` (service=messaging-projector, full dimension set incl. `room_kind`), `AWS/Lambda IteratorAge`, `Errors` | IteratorAge < 5 s, 0 errors |
| DynamoDB | `AWS/DynamoDB ThrottledRequests`, `TransactionConflict` on `lightbulb-dev-messaging` | 0 throttles |
| AppSync | `AWS/AppSync ConnectServerError`, `SubscribeServerError`, `BroadcastEventServerError`, `BroadcastDroppedEvents` (alarmed today) | 0 |
| API GW | `AWS/ApiGateway Count`, `4xx`, `5xx`, `Latency`, `IntegrationLatency` | 5xx = 0 |

**Cost per run** (list prices, verify): ~50k messages. DynamoDB ≈ 6 write units per append → ~0.3 M WRU
≈ $0.40; AppSync Events ≈ $1/M messages → < $0.10; Lambda < $0.10; DLT Fargate ≈ $1/h for 10 tasks.
**Under $5 per run**, plus AOSS OCU-hours if the search collection scales out (watch it).

## 3. Scenario 2 — agent-conversation load

**Question.** What turn rate and how many simultaneous cold workspaces can dev sustain before
replies queue, and where does the time go? Path: human message → `WORK#MESSAGE` row → EventBridge
Pipe → `openclaw-work.fifo` → work Lambda → `runtime_transport.request` (`InvokeAgentRuntime`) →
bridge 202 → turn → gate → OpenRouter → `run.ended` callback → reply write → projector.

| Suspected bottleneck | Why | Signature |
|---|---|---|
| Work Lambda cap 20 | a cold invoke holds a slot ~55 s, so cold throughput ≈ 20/55 ≈ 0.36 wakes/s | `messaging.work.queue_wait` climbs, `ApproximateAgeOfOldestMessage` |
| Single-flight losers | same-owner wakes poll the lease **inside a work Lambda slot**: 20 simultaneous wakes for one owner can idle the whole dev pool for one cold start | queue_wait spike with `Throttles` = 0 |
| Per-agent turn lock | `bridge/turn.py` `_turn_lock`: same agent in two rooms runs one turn later (by design) | reply latency staircase |
| Gate concurrency 25 | each in-flight model call holds a slot; a turn makes several | gate `Throttles`, `ConcurrentExecutions` = 25 |
| Trial cap | $0.25/day per owner | 429 `trial_daily_cap`; `openrouter_capped_day` on the row |
| AgentCore account quotas | concurrent sessions / invoke TPS, **shared with prod** | `ThrottlingException` in work Lambda logs |

**Shape.** O fixture orgs (5 → 30), each provisioned as above (one runtime each). Messages are
**DMs or @mentions** only: both are `user_request`, so no Jev routing call and no ambient wakes.
Body: `Reply with exactly the token <T> and nothing else. Do not use tools.`
(`messaging-concurrency-check.md`), so every reply is checkable for cross-talk. Cold/warm is labelled
only with bridge process-start evidence (`chat-performance.md`), else `unknown`.

| Sub-test | Load |
|---|---|
| 2a warm steady | O = 10, one DM per org per minute, 15 min, after a warm-up turn |
| 2b cold burst | O = 5, 10, 20, 30 cold orgs messaged in the same second (30 crosses the cap of 20) |
| 2c same-owner fan-in | 1 org, 6 agents × 3 rooms, 18 @mentions in one second |
| 2d breadth | 2a at O = 30 with 2 turns/min |

**Metrics and pass bar.** All `LightbulbAI` with `env=dev, service=lightbulb-backend` unless noted.

| Metric | Pass |
|---|---|
| `messaging.work.queue_wait` | p95 < 1 s warm (2a/2d); 2b records the curve |
| `messaging.ingress.latency` | warm p95 < 1 s |
| `messaging.run.start_latency`, `messaging.run.duration` | recorded per sub-test |
| `messaging.reply.latency` | warm p95 < 20 s; cold p95 < 90 s |
| k6 send → reply seen on AppSync | same bars, client side |
| Correct token in correct room | 100% (any miss stops the run — cross-talk) |
| `messaging.run.swept`, `messaging.run.first_turn_failed` | 0 |
| Work DLQ / pipe-failure queue depth (`OpenClawWorkDlqVisibleAlarm`, `OpenClawWorkPipeDlqVisibleAlarm`) | 0 |
| `AWS/Lambda Throttles` on work consumer and gate; gate `ConcurrentExecutions` | recorded; the ceiling we are looking for |
| Tokens per turn: deltas in `lightbulb-dev-usage-counters` (`usage_repo`, written by `usage_service.record_usage`) per fixture owner; $ from each fixture key's OpenRouter usage | used for the cost line below |

**Spend — what is actually possible.**

| Option | Possible? | Verdict |
|---|---|---|
| Cheapest model via `MODEL_POLICY_JSON` | Dev already runs `deepseek-v4-flash` capped at $0.10/$0.20 per M tokens. A zero-price policy routes to free models (`core.mjs` `freePolicy`), but the variable is dev-wide and free models carry OpenRouter's own low per-account request limits, which would become the bottleneck under test. | no |
| BYO-key workspaces | `hermes_profiles._BYO_HERMES_PROVIDER` sends BYO turns straight to Anthropic (`claude-sonnet-5`) or OpenAI (`gpt-5.4`): dearer than the managed model and bypasses the gate, which is part of what we test. | no |
| Dev-only stub | `forward.mjs` hard-codes `https://openrouter.ai/api/v1/chat/completions`; a stub needs new gate code behind an env switch (against the no-flags ruling). | no |
| **Trial fixture orgs on the existing policy** | Real path end to end. Each fixture org's OpenRouter key stops itself at $0.25/day, so worst-case spend is **O × $0.25/day by construction** (30 orgs → $7.50). | **recommended** |

A 10-turn pilot reads tokens per turn from the usage-counter deltas above; above ~$0.005/turn, 2d hits the
cap mid-run and reports 429s, not latency — then lower 2d's rate rather than lift the cap.
**AgentCore cost:** microVM time per org = run + time until stopped; idle timeout is **4 h**
(`IDLE_TIMEOUT_S`), so teardown stops sessions. Budget ≈ O × 1 h of microVM plus DLT Fargate; Jev cost 0.

## 4. Scenario 3 — Claude Code session-sharing load

**Can it run without Macs? Yes, for the backend half.** Read of the code:

| Step | What the Mac does | Synthetic? |
|---|---|---|
| Pair | `POST /desktop/pair` with two PEM Ed25519 public keys under a Clerk JWT; `deviceId = sha256(pubkey)`; the route does not prove key possession (`core/services/desktop_pair.py`) | yes — harness generates keys |
| Enroll | `PUT /desktop/local-sessions/enrollment` (Clerk) | yes |
| Connect | `wss://ws-dev…` with `?token=<device JWT>` (the authorizer's identity source), clientId `lightbulb-local-sessions`; the JWT is **self-minted** EdDSA `{kind:"device", deviceId, owner, iat, exp}`, `exp - iat` ≤ 300 s (`device_token.rs`, `machine_auth.py`) | yes — but needs Ed25519 signing at connect time |
| Register / heartbeat / events / ended / receipt | JSON frames `local.session.register`, `local.device.heartbeat` (every 10 s, `transport.rs`), `local.session.events`, `local.session.ended`, `local.session.receipt` (`local_session_relay.py`) | yes — ids are opaque |
| Screen frames | `local.session.screen`, ≤ 11 KB raw, budget 2 MiB/min per session per backend task (`session_screen.py`) | yes |

So k6 cannot be the Mac (no Ed25519 in its crypto module, and a 300 s token cannot be
pre-minted for a long run). `mac_sim.py` (asyncio + `cryptography` + `websockets`) plays M Macs
from one process: its load is small (M/10 heartbeats/s). Run it from a one-off Fargate task in
dev or the operator's machine; k6 plays the viewers. One synthetic Mac per test user:
`paired_devices` is a map on the owner's **single** containers row (400 KB item limit).

**Not synthesizable:** the real Claude/Codex process, the holder PTY and its xterm snapshots,
the desktop journal's admission semantics (the sim acks receipts but proves only the backend
half), the Tauri transport's backoff/App Nap behaviour, and WebView paint of frames. Those stay
"N viewers on one real session" acceptance, driven by hand on the founder's Mac.

**Question.** Path for a frame: Mac → API GW WebSocket → `ws-message` Lambda (unreserved) →
ALB → backend `/api/v1/ws/message` → `session_screen.forward` queries `VIEWER#` rows → **one
AppSync `publish_user` per viewer per frame**. Path for liveness: heartbeat every 10 s, offline
after `LIVENESS_SECONDS` 30; viewer lease `POST /messaging/sessions/{id}/viewing` every 15 s
(`VIEWER_TTL_S` 45) — **those POSTs count against the 50 rps stage throttle** (300 viewers = 20 rps).

| Sub-test | Load | Probes |
|---|---|---|
| 3a fan-out | 1 Mac, 1 shared session, `output` frames at 4/s × 8 KiB (1.9 MiB/min, under the 2 MiB/min `BUDGET_BYTES_PER_MINUTE`), viewers V = 1, 5, 10, 25, 50 | per-viewer publish cost, backend CPU, frame latency |
| 3a′ limiter | 3a at V = 10 with 6/s × 8 KiB (2.8 MiB/min, ~30% over budget) | drops land before fan-out; latency of the frames that pass |
| 3b relay breadth | M = 10, 25, 50 Macs × 3 sessions, heartbeats 10 s, `events` 1/s | `ws-message` Lambda, DDB writes, heartbeat-ack RTT |
| 3c reconnect storm | 3b at M = 50, then every socket closed and reopened within 1 s | re-register burst, offline flips, conditional-write conflicts |
| 3d viewer churn | 50 viewers open/close the view every 30 s against 3a | `VIEWER#`/`WATCH#` row churn, Mac snapshot resends |

Frame `data` carries `Date.now()` in its terminal text; viewers decode and subtract.

**Metrics and pass bar.** The relay emits no CloudWatch metrics of its own today, so the client
Trends are primary.

| Metric | Pass |
|---|---|
| Frame Mac send → viewer receipt (k6 Trend) | p95 < 1 s at V = 25 |
| Frames delivered / frames sent per viewer (3a only; 3a′ just records the drop rate) | ≥ 99% |
| Heartbeat → `local.device.heartbeat.ack` RTT (sim) | p95 < 1 s |
| Sessions read offline while their Mac is connected (`GET /messaging/sessions`) | 0 |
| `POST …/viewing` | p95 < 500 ms, 0 5xx |
| `AWS/ApiGateway` WS stage: `ConnectCount`, `MessageCount`, `IntegrationError`, `ExecutionError` | errors 0 |
| `AWS/Lambda` `ws-message` `Errors`, `Throttles`, `ConcurrentExecutions` | 0, 0, recorded |
| `AWS/AppSync` broadcast errors / drops; DDB `ThrottledRequests` on messaging + containers tables | 0 |
| Backend `CPUUtilization` (ECS) | recorded; autoscaling at 60% is expected in 3a at V = 50 |

**Cost per run.** 3a at V = 50: 5 frames/s × 50 = 250 AppSync publishes/s × 15 min ≈ 0.23 M
→ ≈ $0.25; Lambda/API GW WS messages ≈ 0.1 M → < $0.50. No AgentCore, no model spend.
**Under $5 per run.**

## 5. Blast-radius controls (all scenarios)

| Control | How |
|---|---|
| Dev only | `fixtures.py` and `mac_sim.py` reuse `dev_only()` (host must be `api-dev`); Clerk instance must be `development`; k6 scripts hard-code `api-dev` and abort otherwise |
| No real users touched | only fixture orgs and load rooms; never `#general`, never the founder or "Jev Listening Test" orgs; message text names no member |
| Shared dev pools | work Lambda (20), gate (25), stage throttle (50 rps) are shared with the team's dev use: run in an announced window |
| Shared prod quotas | stop if account-wide Lambda `ConcurrentExecutions` > 50% of `aws lambda get-account-settings` limit, or any prod page alarm fires |
| Raising the throttle (1d only) | PR changing `THROTTLE_CONFIG.dev` in `api-stack.ts` (+ `cdk synth`, commit `deploy.yml`), merge deploys dev; a revert PR after the run. No console edits (CLAUDE.md Critical Rules) |
| Kill switch | Ctrl-C on k6 (DLT: "Cancel test"); `mac_sim.py` exits on SIGINT and closes sockets; queued agent turns drain by themselves (each org capped at $0.25/day). Emergency only, owner-approved: purge the dev work FIFO (drops every dev user's pending turns) |
| Cleanup | rooms: `DELETE /messaging/rooms/{id}`; scenario 2: `PATCH /debug/provision` per fixture org (stops the warm session — do not wait 4 h); devices: `DELETE /desktop/devices/{id}`. Owner-scoped per fixture org, never a table scan: (1) remove every non-creator member (Clerk BAPI membership delete) and delete those Clerk users; (2) the creator, now sole member, calls `POST /users/me/delete` (`workspace_wipe.wipe_own_workspace`: runtime, Paperclip company + Aurora users, rows); (3) delete any Clerk org/user left. Never call `DELETE /debug/user-data` per member (403 for non-admins). Delete `fixtures.json`. If per-org cleanup leaves residue after a round, the owner permits the full `dev-clean-slate-reset` skill (all four steps) as the fallback — run it only between rounds, never mid-run, and announce it to the team first since it wipes every dev workspace. |
## 6. Owner decisions

1. Target load: messaging rps for the "pass" run (proposal: 40 rps dev, under the throttle), concurrent cold workspaces (proposal: 20), viewers per session (proposal: 25).
2. Budget per run: scenario 2 ceiling O × $0.25/day (proposal O = 20 → $5) plus ≈ 1 h microVM per org.
3. Which first (proposal: a 2-user 1a pilot proving auth/AppSync/clock, then 1, 3, 2 — cheapest and least shared first).
4. If one k6 machine falls short: approve the DLT CloudFormation stack (owner-deployed, outside CDK) in the shared account, and whether to keep it.
5. Approve the trial-org spend path for scenario 2 (no free model, no stub, no BYO).
6. Approve a temporary `THROTTLE_CONFIG.dev` raise for 1d, or skip 1d.
7. Run window: dev pools are shared with the team; prod shares account quotas.
8. Whether to add relay metrics (heartbeat RTT, frame fan-out) to the backend before scenario 3.
