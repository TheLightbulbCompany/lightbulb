# Track F — messaging concurrency, mobile notifications/ordering, test suite

Worktree `.worktrees/messaging-mobile`, base `origin/main` a6104e8aa. One branch per PR.
Delete this file in the PR that ships the last phase.

## Owner rulings (2026-09-22)
- Mobile is NOT blocked: own CI/CD, TestFlight live; JS-only fixes ship as OTA via merge.
- "Better notifications" = **unclear banners** (can't tell room/sender). Nothing else in scope.
- Same-agent-two-chats: **measure first**, then decide (queued-state UI vs per-room trees).
- Automated test suite moves to this track: concurrency harness first; design sign-off
  before building beyond it.

## Known from code (to confirm by measurement)
- One microVM + one bridge process per workspace; one thread per turn (`server.py:208`).
- Different agents run in parallel; the SAME agent is serialized across rooms by
  `_turn_lock(agent_id)` (`bridge/turn.py:1425`, shared working tree). FIFO group = room+agent.

## Phase 1 results (2026-09-22 22:13Z, prod founder workspace, owner-approved)
Harness in EXISTING_USER_ID/EXISTING_ORG_ID mode, Friday + Forge, two #track-f-* channels.

| scenario | agent | reply |
|---|---|---|
| first turn | Friday | 7.2 s |
| warm single | Friday | 4.9 s |
| same agent, two rooms at once | Friday | 5.4 s / 8.5 s: the second queued ~3 s on the per-agent lock |
| two agents, two rooms at once | Friday / Forge | Friday 5.2 s, fine; Forge's cold start took 17 min (below) |
| same room back-to-back | - | not run (stopped at the Forge timeout) |

- Different agents run in parallel; replies land in the right room.
- Same agent across rooms is serialized by `_turn_lock` (bridge/turn.py), as designed. The
  cost is the first turn's length. **Ruling: no lock change.**
- Forge's first turn after wake: `restored profile … 14154 objects in 1023.1s`. The profile is
  517 MB (3 repo checkouts under `.run/`, and caches under `home/.local/share`), restored one
  object at a time. Already fixed by #2908 (parallel restore/save plus cache exclusions);
  "before" posted there. Owed: re-measure Forge's cold turn after the fleet roll.
- `#track-f-a-2ed4d4` / `#track-f-b-2ed4d4` remain in the founder workspace (a room owner
  cannot leave); the owner can archive them.

## Phase 1 — Measure (original plan)
Dev test workspace labelled `track-f`, fresh `+clerk_test` user via Clerk admin API +
sign-in ticket. Scenarios, 3 runs each, record send→run.started, send→first reply,
send→run.ended, overlap, correctness (reply in right room, no cross-talk):
  a. same agent, two rooms concurrently
  b. two agents, two rooms concurrently
  c. same agent, same room, back-to-back
Sources: messaging HTTP API (`/messaging/rooms/*/messages?afterSeq=`), run ledger rows
(`OWNER#/RUN#`), bridge CloudWatch logs. **Output: numbers to owner. Owner sign-off gate.**

## Phase 2 — Concurrency harness (branch feat/messaging-concurrency-harness)
- `apps/frontend/tests/e2e/messaging-concurrency.spec.ts` (new): API-level Playwright spec
  reusing `fixtures/api.ts` (`AuthedFetch`), `fixtures/clerk-admin.ts` (`createSignInToken`),
  `fixtures/user.ts`. Scenarios a–c above; asserts correctness always, latency against
  budgets taken from Phase 1 (logged, soft-fail threshold agreed with owner).
- Runs on-demand via existing `e2e-dev.yml` (grep tag `@concurrency`); no per-PR run (it needs
  a live runtime + LLM spend).
- Dev acceptance: one green run on dev with the timing table attached to the PR.

## Phase 3 — Concurrency fixes: NONE (per-agent lock ruled OK; restore fixed by #2908)
- Likely candidate: same-agent queued turn is invisible → show "waiting on another chat".
  Files TBD (`apps/infra/hermes/bridge/turn.py`, `core/services/openclaw_work.py`, run frames).
- Anything touching the per-owner single-session/S3 single-writer invariant → design note
  to owner before code.

## Phase 4 — Mobile notifications + ordering (reuse rescue commit 74b3d40e1, cherry-pick)
- 4a Ordering: `packages/shared/src/messaging/useRooms.ts` (+test) — live
  `room.message.available` bumps room recency/unread immediately (web + mobile both).
- 4b Banners: `apps/infra/lambda/messaging-projector/index.py` (+test_index.py) — iOS
  `subtitle` = `#channel`/group name, data carries roomName/roomKind/authorDisplayName;
  `apps/mobile/src/lib/notifications.ts` (+test) label helper (drop if no consumer — YAGNI).
- Dev acceptance: simulator screenshot of room jumping to top on a live message; a real
  push banner showing the subtitle (dev build) + projector log line.
- Ships: projector via dev deploy on merge; mobile/shared via OTA on merge. Prod projector
  needs a prod dispatch → owner go-ahead.

## Phase 5 — Test-suite proposal (doc for owner sign-off, no build)
Split: hermetic per-PR (unit/contract: dispatch targets, projector fanout, useRooms reducer,
bridge lock behaviour) vs on-demand dev (concurrency harness, journey spec, push delivery).

## Overlaps to coordinate
- `packages/shared`, `apps/mobile` — onboarding session (e765df).
- `core/services/messaging.py` — Jev session (select_dispatch_targets); I do not plan to edit it.
- Open PRs #2717/#2719 (Forge) touch live chat/runs — check before Phase 3.
