# Apps runs never go silent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** An app owns its run: the backend knows when an app is moving, when it has stopped, and says so; no agent result is dropped without the person being told.

**Architecture:** The app record gains `progress_at` and the agent runs working on it (`run_ids`). The CLI sends the agent run id it was started under; the bridge provides it. A per-app in-process watch marks the app `stuck` when nothing moves for 5 minutes and no linked agent run is open, posts once to the room the request came from, and clears on the next progress. Separately, the room follow-up cap stops dropping agent results silently.

**Tech stack:** Python/FastAPI backend (`apps/backend`), Hermes bridge (`apps/infra/hermes/bridge`, Python), Rust CLI (`TheLightbulbCompany/cli`, worktree `/Users/prasiddhaparthsarthy/Desktop/lbcli-door-frame.nosync`), Next.js Canvas (`apps/frontend`).

**Spec / evidence:** No spec doc. Evidence: run 7 (2 Nov 2026, Demo Lab) — the delegated fix finished 14:06:50Z, `create_follow_up` returned 429 (`FOLLOW_UP_CAP_PER_HOUR = 3`, `apps/backend/core/services/messaging.py`), the bridge (`bridge/followups.py`) logged `refused 429; dropped 1`, and nobody was told. Run 7 also went 36 minutes with no app CLI call while the agent was legitimately coding, so "no CLI call" alone is not "stuck". Stripe Provisioning guide: poll from the backend after 1, 2, 4, 8, 16, 30 s, then every 30 s for up to 15 min; on timeout keep ids and set `requires_review`; never resend the write.

## Global Constraints

- No new dependencies. Reuse existing helpers (`apps_room` for room lines, `messaging_repo` run rows, `apps_repo`, `_patch`).
- In-process asyncio tasks follow the existing `drive()` pattern in `apps_service.py` (one per app, task set, done-callback cleanup) and carry a `ponytail:` comment naming the ceiling (lost on restart; the next read or progress re-arms it).
- Copy is plain, short, and never claims something false. Room line for a stuck app, exactly: `"{display} stopped at {step}: nothing has moved for 5 minutes. Reply here and it will pick up again."` where `{step}` is one of `setting up hosting`, `building`, `checking`, `publishing`.
- Follow-up cap notice, exactly: `"I paused my background work here so it can't loop. Reply here and I'll pick it up again."`
- Waiting on a person (`awaiting_approval`, `awaiting_user_action`) is never "stuck".
- Header name: `X-Lightbulb-Run`. Env var: `LIGHTBULB_RUN_ID`.
- Backend tests in `apps/backend/tests/unit/...`; run `uv run pytest -q --no-cov <files>` and `uv run ruff check` + `ruff format --check` on touched files. CLI: `cargo test` for touched modules, `cargo clippy`. Bridge: its existing pytest suite under `apps/infra/hermes`.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never `git stash`.

## Review Focus

1. A human reply after a cap notice must reset the cap (the count is since the last human message), or the room stays muted.
2. Canvas polling `status` must not count as progress, or a stuck app never looks stuck.
3. An app waiting on approval for hours must not be reported stuck.
4. Two backend instances (rolling deploy) must not post the stuck line twice — use a conditional write.
5. A CLI without `LIGHTBULB_RUN_ID` (developers, old versions) must still work unchanged.

---

### Task 1: Follow-up cap never drops silently (backend)

**Files:** Modify `apps/backend/core/services/messaging.py` (`FOLLOW_UP_CAP_PER_HOUR`, `create_follow_up`), `apps/backend/core/repositories/messaging_repo.py` (`count_recent_follow_ups` or a sibling). Test in the existing messaging service tests.

- [ ] Replace the per-hour cap with a cap on follow-ups for this agent in this room **since the latest human message in the room**: `FOLLOW_UP_CAP_SINCE_HUMAN = 10`. A follow-up while under the cap behaves exactly as today.
- [ ] When capped: post the cap notice (Global Constraints copy) once into the room as the agent (author kind `agent`, the agent's display name, threaded under the trigger message's thread root), idempotent per (room, agent, latest human message) so retries do not post twice; then raise `RATE_LIMITED` as today.
- [ ] Tests: 4 follow-ups after one human message are all accepted (the run 7 case); the 11th is refused and exactly one notice is posted even if called twice; a new human message resets the count.

### Task 2: Provisioning drive follows Stripe's schedule (backend)

**Files:** Modify `apps/backend/core/services/apps_service.py` (`_DRIVE_EVERY_S`, `_DRIVE_STEPS`, `drive`, `_drive`). Test in `tests/unit/services/test_apps_service.py` (replace `test_drive_steps_the_app_until_its_preview_is_up_and_once_per_app` as needed).

- [ ] Waits: 1, 2, 4, 8, 16, 30 s, then every 30 s, stopping after 15 minutes total or when state is in `ready|live|failed|destroyed`. Add ±10 % jitter. Expose the schedule as a module constant tests can patch.
- [ ] Tests: the schedule's first six waits and total cap; stops early on `ready`; one drive per app.

### Task 3: The app owns its run — progress, linked runs, stuck (backend)

**Files:** `apps/backend/core/services/apps_service.py`, `apps/backend/routers/apps.py` (read `X-Lightbulb-Run`), `apps/backend/core/repositories/apps_repo.py` if a conditional write helper is needed, `apps/backend/core/services/apps_room.py` (the room line). Tests in `tests/unit/services/test_apps_service.py` and the apps router tests.

**Interfaces produced:** app record fields `progress_at: str` (ISO), `run_ids: list[str]` (last 10, newest last), `stuck: {"at": str, "step": str} | None`; `AppStatus` gains `stuck`.

- [ ] Progress: stamp `progress_at` on every builder write (create, source upload/`up`, evidence/verify/review record, publish, rollback, settings change) and whenever a step really moves (a workflow row changes state, a deployment changes state, `_record_state` changes the app state). A plain `status` read with nothing moving must NOT stamp.
- [ ] Linked runs: builder routes read `X-Lightbulb-Run`; when present and not already last, append to `run_ids` (cap 10). Absent header = no change.
- [ ] Watch: started on app create and re-armed on any progress (one per app, `drive()` pattern). Every 60 s, until state is `ready|live|failed|destroyed` or 3 hours pass: the app is stuck when `now - progress_at >= 300 s`, it is not waiting on a person, and none of its `run_ids` is an open agent run (`messaging_repo.get_run(owner_id, run_id)`: open and heartbeat fresher than 5 minutes — read the run row shape to decide the exact fields). With no `run_ids` at all, use 20 minutes instead of 5 (old CLI). When stuck: conditional write `stuck` (only if not already set for this `progress_at`), then post the stuck room line once via `apps_room` into the app's `room_id`. Next progress clears `stuck`.
- [ ] `status`/`_view` include `stuck`.
- [ ] Tests: status read does not stamp; `up` stamps and records the header run; stuck after 5 min with no open run; not stuck while a linked run is open; not stuck while `awaiting_approval`; 20-minute fallback without run ids; second watcher/instance posts nothing (conditional write); progress clears `stuck`.

### Task 4: Canvas shows a stopped app (frontend)

**Files:** `apps/frontend/src/components/canvas/AppView.tsx` (and the app type it reads), plus its existing test if one exists.

- [ ] When the app status has `stuck`, the preview notice line reads `Stopped at {step}. Nothing has moved for 5 minutes. Reply in the room and it will pick up again.` in place of the usual notice; nothing else changes.
- [ ] Typecheck and lint the frontend package for touched files.

### Task 5: The bridge gives tools the run id (Hermes bridge)

**Files:** `apps/infra/hermes/bridge/` (where a turn starts tool subprocesses / sets their environment). Tests in the bridge's existing suite.

- [ ] For every agent turn that has a backend run id, tool subprocesses (terminal tool, and anything that spawns `lightbulb`) get `LIGHTBULB_RUN_ID=<run id>`; unset otherwise. Find where the run id is known in `turn.py`/`server.py`; do not change any other env.
- [ ] Test: a turn with run id R exposes `LIGHTBULB_RUN_ID=R` to the terminal tool's environment; a turn without one does not set it.

### Task 6: The CLI sends the run id (cli repo)

**Repo:** `/Users/prasiddhaparthsarthy/Desktop/lbcli-door-frame.nosync` on a new branch from `origin/main`.

- [ ] Every request the CLI makes to the Lightbulb backend carries `X-Lightbulb-Run: $LIGHTBULB_RUN_ID` when the env var is set and non-empty; nothing changes when it is not.
- [ ] Test at the HTTP client layer: header present with the env var, absent without it.
