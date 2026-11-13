# Load-test harness (dev only)

Two scenarios: (1) messaging, humans only; (2) agent conversations. Per-sub-test commands and client-side thresholds: `k6/README.md`.
The design spec was deleted when this shipped (git history keeps it); this file is the record.

> **Never run anything here without the owner's go for that run.** Dev pools (work Lambda 20, gate 25,
> stage throttle 50 rps) are shared with the team, and Lambda/API GW/AppSync/AgentCore quotas are shared
> with **prod** (one AWS account). Announce the window first.

| Piece | What |
|---|---|
| `fixtures.py` | `setup` creates `load-NNNN+clerk_test@example.com` users and `load-org-NNN` workspaces and waits until each is ready; `run -- <cmd>` signs them in, writes `fixtures.json` (0600), runs the command, deletes the file; `teardown` removes every fixture |
| `k6/*.js` | the two scenarios; read `FIXTURES`, hard-code `api-dev` |

Both refuse anything but `api-dev` and a `development` Clerk instance. Every command has `--dry-run`
(prints the plan, calls nothing).

## Prerequisites

- `k6` on the operator machine (`brew install k6`). Nothing else: no npm packages.
- AWS SSO for the dev Clerk secret: `aws sso login --profile isol8-admin`. `fixtures.py` reads
  `lightbulb/dev/clerk_secret_key` from Secrets Manager at run time. Never copy it into a file or commit it.
- `cd apps/backend && uv sync` (uses the backend's `httpx`, `PyJWT`, `cryptography`, `websockets`).
- No Clerk frontend API is used: sessions and JWTs come from the Backend API, which creates
  sessions only on a development instance (see "How fixture users get their tokens").
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

### How fixture users get their tokens

`run` never signs in through Clerk's frontend API (it rate-limits sign-ins per IP: scenario 1a's 16
back-to-back sign-ins got a 429 on 2026-10-08). It creates each user's session with the Backend API
(`POST /v1/sessions {user_id, active_organization_id}` — Clerk documents it as for testing only and
**not available on production instances**; this harness is dev only), then mints ONE API JWT and ONE
`lightbulb-appsync` JWT per user with `expires_in_seconds = --ttl` (default 7200; Clerk allows
30–315360000). Backend API calls are paced at ≤ 5/s and wait out a 429's Retry-After. The first JWT
of every user must carry the fixture org in `o.id`, or `run` refuses. Nothing refreshes a token
during a run, so `--ttl` must be longer than the whole command; k6 refuses to start on an expired
file. `fixtures.json` holds only those JWTs (no Clerk secret, no session handle) and is deleted when
the command ends.

## Scenarios

| # | Question | Cost per run (list prices, verify) |
|---|---|---|
| 1 messaging | How many msg/s can one room, and the whole dev stack, accept and deliver live? Suspects in order: the per-room `lastSeq` transaction (6 tries, no backoff), the 50 rps stage throttle, the projector (one invocation per shard, batch 10), AppSync fan-out, the search indexer | < $5 (~50k messages: DynamoDB ~$0.40, AppSync < $0.10, Lambda < $0.10), plus AOSS OCU-hours if search scales out |
| 2 agents | What turn rate and how many simultaneous cold workspaces before replies queue, and where does the time go? Suspects: work Lambda cap 20 (cold ≈ 0.36 wakes/s), single-flight losers polling inside a slot, the per-agent turn lock, gate concurrency 25, the trial cap, AgentCore quotas shared with prod | ≤ orgs × $0.25/day (trial keys stop themselves) + ~1 h microVM per org. A 10-turn pilot reads tokens/turn; above ~$0.005/turn, 2d hits the cap mid-run: lower its rate, never lift the cap |

Order (owner's proposal): 2-user pilot, then 1, 2 — cheapest and least shared first.

## Facts every run inherits (verify before quoting; the sources move)

| Fact | Value | Source |
|---|---|---|
| Dev HTTP API throttle | 100 burst / 50 rps, stage-wide, shared by every dev user | `api-stack.ts` `THROTTLE_CONFIG.dev` |
| Work consumer | batch 1, `maxConcurrency` 20, timeout 180 s; a cold wake holds a slot ~55 s | `service-stack.ts` `WorkConsumer` |
| Projector | DDB stream, batch 10, **no reserved concurrency** (shares prod's Lambda pool) | `api-stack.ts` `MessagingProjector` |
| openrouter-gate | reserved concurrency 25, one slot per in-flight model call | `openrouter-gate-stack.ts` |
| Trial key | $0.25/day per owner; over it the gate answers 429 `trial_daily_cap`. Scenario 2 spend is at most orgs × $0.25/day | CLAUDE.md "Managed inference" |
| Room size | `messaging_repo.MAX_ROOM_MEMBERS` = 32 | |
| One AWS account for dev AND prod | Lambda unreserved concurrency, API GW, AppSync and AgentCore quotas are shared with prod | CLAUDE.md "AWS Configuration" |

Never post in `#general` (all six agents listen there). Message text never opens with a member's name
(a name-addressed open wakes Jev). Scenario 2 uses only @mentions, so no Jev call and no ambient wake.

## Server-side metrics to read (CloudWatch, `LightbulbAI`, `env=dev`)

| Scenario | Watch | Pass |
|---|---|---|
| 1 | `messaging.projector.notify.latency` (service=messaging-projector, full dimension set incl. `room_kind`); projector `IteratorAge`, `Errors`; `lightbulb-dev-messaging` `ThrottledRequests`/`TransactionConflict`; AppSync `ConnectServerError`, `SubscribeServerError`, `BroadcastEventServerError`, `BroadcastDroppedEvents`; API GW `5xx`, `Latency`; index DLQ, `MessageSearchBatchStaleAlarm` | IteratorAge < 5 s, 0 errors, 0 throttles, 5xx = 0 |
| 2 | `messaging.work.queue_wait` (warm p95 < 1 s), `messaging.ingress.latency` (warm p95 < 1 s), `messaging.run.start_latency`, `messaging.run.duration`, `messaging.reply.latency` (warm p95 < 20 s, cold < 90 s), `messaging.run.swept` and `messaging.run.first_turn_failed` (0), work DLQ + pipe DLQ (0), Lambda `Throttles` on work consumer and gate, gate `ConcurrentExecutions`, `lightbulb-dev-usage-counters` deltas per fixture owner | as listed; throttles are the ceiling being looked for |

Cold vs warm (scenario 2) is labelled only from bridge process-start evidence (`docs/runbooks/chat-performance.md`), else `unknown`.

## Kill switch

- **Ctrl-C** once: k6 stops and prints its summary; `fixtures.py` deletes `fixtures.json` at once.
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
