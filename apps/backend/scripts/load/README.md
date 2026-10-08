# Load-test harness (dev only)

Three scenarios: (1) messaging, humans only; (2) agent conversations; (3) Claude Code session
sharing, with synthetic Macs. Per-sub-test commands and client-side thresholds: `k6/README.md`.
The design spec was deleted when this shipped (git history keeps it); this file is the record.

> **Never run anything here without the owner's go for that run.** Dev pools (work Lambda 20, gate 25,
> stage throttle 50 rps) are shared with the team, and Lambda/API GW/AppSync/AgentCore quotas are shared
> with **prod** (one AWS account). Announce the window first.

| Piece | What |
|---|---|
| `fixtures.py` | `setup` creates `load-NNNN+clerk_test@example.com` users and `load-org-NNN` workspaces and waits until each is ready; `run -- <cmd>` signs them in, writes `fixtures.json` (0600), runs the command, deletes the file; `teardown` removes every fixture |
| `k6/*.js` | the three scenarios; read `FIXTURES`, hard-code `api-dev` |
| `mac_sim.py` | synthetic Macs for scenario 3; runs the viewers' k6 command itself |

All three refuse anything but `api-dev` and a `development` Clerk instance. Every command has `--dry-run`
(prints the plan, calls nothing).

## Prerequisites

- `k6` on the operator machine (`brew install k6`). Nothing else: no npm packages.
- AWS SSO for the dev Clerk secret: `aws sso login --profile isol8-admin`. `fixtures.py` reads
  `lightbulb/dev/clerk_secret_key` from Secrets Manager at run time. Never copy it into a file or commit it.
- `cd apps/backend && uv sync` (uses the backend's `httpx`, `PyJWT`, `cryptography`, `websockets`).
- Clerk's dev instance is documented at 100 users; `setup` refuses past `CLERK_USER_CAP` (default 100).
  Verify the real cap before a run larger than the pilot.

## Pilot (2 users, 60 s) — the first run, proves auth, AppSync and the clocks

```bash
cd apps/backend
uv run python -m scripts.load.fixtures setup --orgs 1 --users-per-org 2 --dry-run   # read the plan
uv run python -m scripts.load.fixtures setup --orgs 1 --users-per-org 2              # waits for ready
uv run python -m scripts.load.fixtures run --orgs 1 --users-per-org 2 -- \
  k6 run -e SEND_S=60 -e STAGES='[{"target":1,"duration":"60s"}]' scripts/load/k6/messaging.js
uv run python -m scripts.load.fixtures teardown
```

The pilot also measures what the spec leaves open: Clerk FAPI per-IP limits, whether one client JWT
used by several k6 VUs keeps rotating cleanly, and whether AppSync keeps a subscription open past its
token's 60 s life (the harness refreshes HTTP tokens every 40 s but does not re-subscribe).

## Scenarios

| # | Question | Cost per run (list prices, verify) |
|---|---|---|
| 1 messaging | How many msg/s can one room, and the whole dev stack, accept and deliver live? Suspects in order: the per-room `lastSeq` transaction (6 tries, no backoff), the 50 rps stage throttle, the projector (one invocation per shard, batch 10), AppSync fan-out, the search indexer | < $5 (~50k messages: DynamoDB ~$0.40, AppSync < $0.10, Lambda < $0.10), plus AOSS OCU-hours if search scales out |
| 2 agents | What turn rate and how many simultaneous cold workspaces before replies queue, and where does the time go? Suspects: work Lambda cap 20 (cold ≈ 0.36 wakes/s), single-flight losers polling inside a slot, the per-agent turn lock, gate concurrency 25, the trial cap, AgentCore quotas shared with prod | ≤ orgs × $0.25/day (trial keys stop themselves) + ~1 h microVM per org. A 10-turn pilot reads tokens/turn; above ~$0.005/turn, 2d hits the cap mid-run: lower its rate, never lift the cap |
| 3 sessions | What does one shared session's frame fan-out (one AppSync publish per viewer per frame) and many Macs' relay traffic cost? Backend half only: no real Claude/Codex, holder, journal or Tauri transport | < $5 (3a at V = 50 ≈ 0.23 M publishes); no model spend |

Order (owner's proposal): 2-user pilot, then 1, 3, 2 — cheapest and least shared first.

## Facts every run inherits (verify before quoting; the sources move)

| Fact | Value | Source |
|---|---|---|
| Dev HTTP API throttle | 100 burst / 50 rps, stage-wide, shared by every dev user | `api-stack.ts` `THROTTLE_CONFIG.dev` |
| Work consumer | batch 1, `maxConcurrency` 20, timeout 180 s; a cold wake holds a slot ~55 s | `service-stack.ts` `WorkConsumer` |
| Projector | DDB stream, batch 10, **no reserved concurrency** (shares prod's Lambda pool) | `api-stack.ts` `MessagingProjector` |
| openrouter-gate | reserved concurrency 25, one slot per in-flight model call | `openrouter-gate-stack.ts` |
| Trial key | $0.25/day per owner; over it the gate answers 429 `trial_daily_cap`. Scenario 2 spend is at most orgs × $0.25/day | CLAUDE.md "Managed inference" |
| Room size | `messaging_repo.MAX_ROOM_MEMBERS` = 32 | |
| Relay | frames ≤ 16 KiB envelope, screen ≤ 11 KiB raw, 2 MiB/min per session per backend task; heartbeat 10 s, offline after 30 s; viewer lease 45 s, renewed every 15 s (these POSTs count against the 50 rps) | `local_session_relay.py`, `session_screen.py` |
| One AWS account for dev AND prod | Lambda unreserved concurrency, API GW, AppSync and AgentCore quotas are shared with prod | CLAUDE.md "AWS Configuration" |

Never post in `#general` (all six agents listen there). Message text never opens with a member's name
(a name-addressed open wakes Jev). Scenario 2 uses only @mentions, so no Jev call and no ambient wake.

## Server-side metrics to read (CloudWatch, `LightbulbAI`, `env=dev`)

| Scenario | Watch | Pass |
|---|---|---|
| 1 | `messaging.projector.notify.latency` (service=messaging-projector, full dimension set incl. `room_kind`); projector `IteratorAge`, `Errors`; `lightbulb-dev-messaging` `ThrottledRequests`/`TransactionConflict`; AppSync `ConnectServerError`, `SubscribeServerError`, `BroadcastEventServerError`, `BroadcastDroppedEvents`; API GW `5xx`, `Latency`; index DLQ, `MessageSearchBatchStaleAlarm` | IteratorAge < 5 s, 0 errors, 0 throttles, 5xx = 0 |
| 2 | `messaging.work.queue_wait` (warm p95 < 1 s), `messaging.ingress.latency` (warm p95 < 1 s), `messaging.run.start_latency`, `messaging.run.duration`, `messaging.reply.latency` (warm p95 < 20 s, cold < 90 s), `messaging.run.swept` and `messaging.run.first_turn_failed` (0), work DLQ + pipe DLQ (0), Lambda `Throttles` on work consumer and gate, gate `ConcurrentExecutions`, `lightbulb-dev-usage-counters` deltas per fixture owner | as listed; throttles are the ceiling being looked for |
| 3 | `session.relay.*`, `session.screen.*` (`frame.forwarded`, `frame.dropped`, `publish.failed`, `viewers`), `session.command.dispatch.latency`; API GW WS `IntegrationError`, `ExecutionError`; `ws-message` `Errors`, `Throttles`, `ConcurrentExecutions`; DDB throttles on messaging + containers; backend ECS `CPUUtilization` | errors 0; CPU recorded |

Cold vs warm (scenario 2) is labelled only from bridge process-start evidence (`docs/runbooks/chat-performance.md`), else `unknown`.

## Kill switch

- **Ctrl-C** once: k6 stops and prints its summary; `fixtures.py` deletes `fixtures.json` at once;
  `mac_sim.py` ends its sessions, closes its sockets, unpairs its devices and deletes `mac_sessions.json`.
  A second Ctrl-C hard-stops k6.
- Queued agent turns drain by themselves; each fixture org stops itself at its $0.25/day trial cap.
- Emergency only, owner-approved: purge the dev work FIFO (drops every dev user's pending turns).
- Stop if account-wide Lambda `ConcurrentExecutions` passes 50% of `aws lambda get-account-settings`,
  or any prod page alarm fires.

## Cleanup

`fixtures.py teardown` per fixture org: removes and deletes every non-creator member, waits until Clerk
lists only the creator, then the creator calls `POST /users/me/delete {"confirm_email": …}` (the sole
member's workspace wipe: runtime, Paperclip company, rows), retrying 409 for up to 2 minutes, then
deletes any Clerk org or `load-*` user left. It refuses an org with any member that is not a load
fixture. Load rooms go with the workspace. If residue survives a round, the owner permits the
`dev-clean-slate-reset` skill between rounds only, announced to the team first.
