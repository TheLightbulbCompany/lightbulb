# k6 scenarios

Facts, blast radius and server-side metrics: `../README.md`. Every command runs from `apps/backend`
under `fixtures.py run`, which mints each fixture user's API + AppSync JWTs server-side for `--ttl` seconds
(default 7200: longer than the whole command, nothing refreshes them), sets `FIXTURES`, and deletes the file after.
`F` below is short for `uv run python -m scripts.load.fixtures`. **Never run any of these without the
owner's go for that run** (see `../README.md`).

AppSync redeliveries are dropped by `eventId` before any metric (counted in `dupes`).

Common env: `SEND_S` = seconds of sending (senders start at 30 s; receivers hold `SEND_S + 90` s: the 30 s head start plus a 60 s drain),
`STAGES` = k6 `ramping-arrival-rate` stages (JSON), `TIME_UNIT` (default `1s`), `PRE_VUS`/`MAX_VUS`
(override the spec's sizing), `RUN_ID` (names the load rooms; set the same value on every DLT task),
`TASK_INDEX`/`TASK_COUNT` (DLT: each task keeps its slice of senders and receivers; **`STAGES` are per
task**, so divide the target rate by `TASK_COUNT` yourself).

## Scenario 1: messaging (`messaging.js`)

Env: `ROOMS`, `ROOM_SIZE` (≤ 32), `SENDERS_PER_ROOM`. Room `r` holds fixture users
`r*ROOM_SIZE …`; its first `SENDERS_PER_ROOM` send, the rest subscribe to the room channel.

| Sub-test | Fixtures | Command |
|---|---|---|
| Pilot | `--orgs 1 --users-per-org 2` | `k6 run -e SEND_S=60 -e STAGES='[{"target":1,"duration":"60s"}]' scripts/load/k6/messaging.js` |
| 1a hot room | `--users-per-org 17` | `-e ROOM_SIZE=17 -e SENDERS_PER_ROOM=16 -e SEND_S=900 -e STAGES='[{"target":1,"duration":"0s"},{"target":1,"duration":"3m"},{"target":2,"duration":"0s"},{"target":2,"duration":"3m"},{"target":4,"duration":"0s"},{"target":4,"duration":"3m"},{"target":8,"duration":"0s"},{"target":8,"duration":"3m"},{"target":16,"duration":"0s"},{"target":16,"duration":"3m"}]'` |
| 1b fan-out | `--users-per-org 32` | one run per N in 1, 10, 20, 31: `-e ROOM_SIZE=$((N+1)) -e SENDERS_PER_ROOM=1 -e SEND_S=180 -e STAGES='[{"target":1,"duration":"0s"},{"target":1,"duration":"3m"}]'` |
| 1b′ volume | `--users-per-org 96` (needs Clerk cap headroom; else `ROOM_SIZE=26` on 78) | `-e ROOMS=3 -e ROOM_SIZE=32 -e SENDERS_PER_ROOM=1 -e SEND_S=180 -e STAGES='[{"target":3,"duration":"0s"},{"target":3,"duration":"3m"}]'` |
| 1c breadth | `--users-per-org 80` | `-e ROOMS=20 -e ROOM_SIZE=4 -e SENDERS_PER_ROOM=1 -e SEND_S=1200 -e STAGES='[{"target":5,"duration":"0s"},{"target":40,"duration":"10m"},{"target":40,"duration":"10m"}]'` |
| 1d | owner-gated: only after a CDK PR raises `THROTTLE_CONFIG.dev` | 1c with a higher top stage |

1a's senders are picked per iteration (`iterationInTest % 16`), so "2 senders" is 2 msg/s spread over
the 16 sender users, as the spec's shape says.

Thresholds (built in): `http_req_failed` rate < 0.5% (429s are not failures; they count in `http_429`),
send ack `http_req_duration{name:POST /messaging/rooms/:id/messages}` p95 < 500 ms,
`live_delivery_ms` p95 < 2 s, `complete_after_reconcile` = 100%, `receivers_unsubscribed` = 0 (a receiver not
subscribed before the first send fails the run and its messages count as missed), `completeness_read_failed` = 0 (the
end-of-run room read is retried 3 times; if it still fails the run fails, never guesses the last seq). Recorded: `live_completeness` (before
any `afterSeq` read), `gap_reconciles`, `messages_sent`. Only receivers subscribed before the first
send count. The hot-room ceiling (first 1a step with 5xx) is read off the run, not a threshold.

## Scenario 2: agents (`agents.js`)

With `TASK_COUNT > 1`, task `t` sends global iterations `t, t+n, t+2n, …` (`taskIteration`), so tasks
interleave over the orgs instead of all starting at org 0.

Every org gets `AGENT_ROOMS` (default 1) load rooms with its creator and the six starter agents on
`mentions`. Iteration `i` messages org `i % O`, then rooms, then agents. Each message is
`@<agent> Reply with exactly the token LT<ms>x<room>x<agent>x<rand> …`; a reply carrying a token for
another room or agent increments `crosstalk` and aborts the run.

| Sub-test | Fixtures | Command |
|---|---|---|
| 2a warm steady | `--orgs 10 --users-per-org 1` | first a 1-turn-per-org warm-up (`SEND_S=60`, `TIME_UNIT=1m`, target 10), then `-e TIME_UNIT=1m -e SEND_S=900 -e STAGES='[{"target":10,"duration":"0s"},{"target":10,"duration":"15m"}]'` |
| 2b cold burst | `--orgs O` (5, 10, 20, 30), cold (idle > 4 h or after `PATCH /debug/provision`) | `-e SEND_S=120 -e REPLY_P95_MS=90000 -e PRE_VUS=O -e MAX_VUS=O -e STAGES='[{"target":O,"duration":"0s"},{"target":O,"duration":"1s"},{"target":0,"duration":"0s"},{"target":0,"duration":"119s"}]'` |
| 2c fan-in | `--orgs 1 --users-per-org 1` | `-e AGENT_ROOMS=3 -e SEND_S=120 -e PRE_VUS=18 -e MAX_VUS=18 -e STAGES='[{"target":18,"duration":"0s"},{"target":18,"duration":"1s"},{"target":0,"duration":"0s"},{"target":0,"duration":"119s"}]'` |
| 2d breadth | `--orgs 30 --users-per-org 1` | `-e TIME_UNIT=1m -e SEND_S=900 -e STAGES='[{"target":60,"duration":"0s"},{"target":60,"duration":"15m"}]'` |

Thresholds: `crosstalk` = 0 (abort on fail), `http_req_failed` < 0.5%, `reply_seen_ms` p95 <
`REPLY_P95_MS` (20 s warm, 90 s cold), `reply_complete` ≥ 99% (one sample per accepted turn, read from
the room's canonical history at the end: did its agent answer with its token, in its room, inside the
run's 60 s drain?), `completeness_read_failed` = 0. Recorded: `turns_sent`, `replies_ok`,
`replies_without_token`. The server-side bars (`messaging.work.queue_wait`,
`messaging.reply.latency`, run sweeps, DLQs, Lambda throttles, usage-counter deltas) are read from
CloudWatch (see `../README.md`); cold vs warm is labelled from bridge process-start logs only.
